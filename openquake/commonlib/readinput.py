#  -*- coding: utf-8 -*-
#  vim: tabstop=4 shiftwidth=4 softtabstop=4

#  Copyright (c) 2014-2015, GEM Foundation

#  OpenQuake is free software: you can redistribute it and/or modify it
#  under the terms of the GNU Affero General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.

#  OpenQuake is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.

#  You should have received a copy of the GNU Affero General Public License
#  along with OpenQuake.  If not, see <http://www.gnu.org/licenses/>.

import os
import csv
import gzip
import zipfile
import logging
import operator
import tempfile
import collections
import numpy
from shapely import wkt, geometry

from openquake.hazardlib import geo, site, correlation, imt
from openquake.hazardlib.calc.hazard_curve import zero_curves
from openquake.risklib import workflows, riskinput

from openquake.commonlib.datastore import DataStore, Fake
from openquake.commonlib.oqvalidation import OqParam
from openquake.commonlib.node import read_nodes, LiteralNode, context
from openquake.commonlib import nrml, valid, logictree, InvalidFile, parallel
from openquake.commonlib.riskmodels import get_risk_models
from openquake.baselib.general import groupby, AccumDict, writetmp
from openquake.baselib.performance import DummyMonitor
from openquake.baselib.python3compat import configparser

from openquake.commonlib import source, sourceconverter

# the following is quite arbitrary, it gives output weights that I like (MS)
NORMALIZATION_FACTOR = 1E-2
MAX_SITE_MODEL_DISTANCE = 5  # km, given by Graeme Weatherill

info_dt = numpy.dtype([('input_weight', float),
                       ('output_weight', float),
                       ('n_imts', numpy.uint32),
                       ('n_levels', numpy.uint32),
                       ('n_sites', numpy.uint32),
                       ('n_sources', numpy.uint32),
                       ('n_realizations', numpy.uint32)])


class DuplicatedPoint(Exception):
    """
    Raised when reading a CSV file with duplicated (lon, lat) pairs
    """


def collect_files(dirpath, cond=lambda fullname: True):
    """
    Recursively collect the files contained inside dirpath.

    :param dirpath: path to a readable directory
    :param cond: condition on the path to collect the file
    """
    files = []
    for fname in os.listdir(dirpath):
        fullname = os.path.join(dirpath, fname)
        if os.path.isdir(fullname):  # navigate inside
            files.extend(collect_files(fullname))
        else:  # collect files
            if cond(fullname):
                files.append(fullname)
    return files


def extract_from_zip(path, candidates):
    """
    Given a zip archive and a function to detect the presence of a given
    filename, unzip the archive into a temporary directory and return the
    full path of the file. Raise an IOError if the file cannot be found
    within the archive.

    :param path: pathname of the archive
    :param candidates: list of names to search for
    """
    temp_dir = tempfile.mkdtemp()
    with zipfile.ZipFile(path) as archive:
        archive.extractall(temp_dir)
    return [f for f in collect_files(temp_dir)
            if os.path.basename(f) in candidates]


def get_params(job_inis):
    """
    Parse one or more INI-style config files.

    :param job_inis:
        List of configuration files (or list containing a single zip archive)
    :returns:
        A dictionary of parameters
    """
    if len(job_inis) == 1 and job_inis[0].endswith('.zip'):
        job_inis = extract_from_zip(
            job_inis[0], ['job_hazard.ini', 'job_haz.ini',
                          'job.ini', 'job_risk.ini'])

    not_found = [ini for ini in job_inis if not os.path.exists(ini)]
    if not_found:  # something was not found
        raise IOError('File not found: %s' % not_found[0])

    cp = configparser.ConfigParser()
    cp.read(job_inis)

    # drectory containing the config files we're parsing
    job_ini = os.path.abspath(job_inis[0])
    base_path = os.path.dirname(job_ini)
    params = dict(base_path=base_path, inputs={'job_ini': job_ini})

    for sect in cp.sections():
        for key, value in cp.items(sect):
            if key.endswith(('_file', '_csv')):
                input_type, _ext = key.rsplit('_', 1)
                path = value if os.path.isabs(value) else os.path.join(
                    base_path, value)
                params['inputs'][input_type] = possibly_gunzip(path)
            else:
                params[key] = value

    # populate the 'source' list
    smlt = params['inputs'].get('source_model_logic_tree')
    if smlt:
        params['inputs']['source'] = [
            os.path.join(base_path, src_path)
            for src_path in source.collect_source_model_paths(smlt)]

    return params


def get_oqparam(job_ini, pkg=None, calculators=None, hc_id=None):
    """
    Parse a dictionary of parameters from an INI-style config file.

    :param job_ini:
        Path to configuration file/archive or dictionary of parameters
    :param pkg:
        Python package where to find the configuration file (optional)
    :param calculators:
        Sequence of calculator names (optional) used to restrict the
        valid choices for `calculation_mode`
    :param hc_id:
        Not None only when called from a post calculation
    :returns:
        An :class:`openquake.commonlib.oqvalidation.OqParam` instance
        containing the validate and casted parameters/values parsed from
        the job.ini file as well as a subdictionary 'inputs' containing
        absolute paths to all of the files referenced in the job.ini, keyed by
        the parameter name.
    """
    # UGLY: this is here to avoid circular imports
    from openquake.calculators import base

    OqParam.calculation_mode.validator.choices = tuple(
        calculators or base.calculators)

    if not isinstance(job_ini, dict):
        basedir = os.path.dirname(pkg.__file__) if pkg else ''
        job_ini = get_params([os.path.join(basedir, job_ini)])

    oqparam = OqParam(**job_ini)
    oqparam.validate()
    return oqparam


def get_mesh(oqparam):
    """
    Extract the mesh of points to compute from the sites,
    the sites_csv, or the region.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    if oqparam.sites:
        lons, lats, depths = zip(*sorted(oqparam.sites))
        return geo.Mesh(numpy.array(lons), numpy.array(lats),
                        numpy.array(depths))
    elif 'sites' in oqparam.inputs:
        csv_data = open(oqparam.inputs['sites'], 'U').read()
        coords = valid.coordinates(
            csv_data.strip().replace(',', ' ').replace('\n', ','))
        lons, lats, depths = zip(*sorted(coords))
        return geo.Mesh(numpy.array(lons), numpy.array(lats),
                        numpy.array(depths))
    elif oqparam.region:
        # close the linear polygon ring by appending the first
        # point to the end
        firstpoint = geo.Point(*oqparam.region[0])
        points = [geo.Point(*xy) for xy in oqparam.region] + [firstpoint]
        try:
            mesh = geo.Polygon(points).discretize(oqparam.region_grid_spacing)
            lons, lats = zip(*sorted(zip(mesh.lons, mesh.lats)))
            return geo.Mesh(numpy.array(lons), numpy.array(lats))
        except:
            raise ValueError(
                'Could not discretize region %(region)s with grid spacing '
                '%(region_grid_spacing)s' % vars(oqparam))
    elif 'gmfs' in oqparam.inputs:
        return get_gmfs(oqparam)[0].mesh
    elif oqparam.hazard_calculation_id:
        sitemesh = DataStore(oqparam.hazard_calculation_id)['sitemesh']
        return geo.Mesh(sitemesh['lon'], sitemesh['lat'])
    elif 'exposure' in oqparam.inputs:
        # the mesh is extracted from get_sitecol_assets
        return
    elif 'site_model' in oqparam.inputs:
        coords = [(param.lon, param.lat) for param in get_site_model(oqparam)]
        lons, lats = zip(*sorted(coords))
        return geo.Mesh(numpy.array(lons), numpy.array(lats))


def sitecol_from_coords(oqparam, coords):
    """
    Return a SiteCollection instance from an ordered set of coordinates
    """
    lons, lats = zip(*coords)
    return site.SiteCollection.from_points(
        lons, lats, range(len(lons)), oqparam)


def get_site_model(oqparam):
    """
    Convert the NRML file into an iterator over 6-tuple of the form
    (z1pt0, z2pt5, measured, vs30, lon, lat)

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    for node in read_nodes(oqparam.inputs['site_model'],
                           lambda el: el.tag.endswith('site'),
                           source.nodefactory['siteModel']):
        yield valid.site_param(**node.attrib)


def get_site_collection(oqparam, mesh=None, site_ids=None,
                        site_model_params=None):
    """
    Returns a SiteCollection instance by looking at the points and the
    site model defined by the configuration parameters.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param mesh:
        a mesh of hazardlib points; if None the mesh is
        determined by invoking get_mesh
    :param site_ids:
        a list of integers to identify the points; if None, a
        range(1, len(points) + 1) is used
    :param site_model_params:
        object with a method .get_closest returning the closest site
        model parameters
    """
    if mesh is None:
        mesh = get_mesh(oqparam)
    if mesh is None:
        return
    site_ids = site_ids or list(range(len(mesh)))
    if oqparam.inputs.get('site_model'):
        if site_model_params is None:
            # read the parameters directly from their file
            site_model_params = geo.geodetic.GeographicObjects(
                get_site_model(oqparam))
        sitecol = []
        for i, pt in zip(site_ids, mesh):
            param, dist = site_model_params.\
                get_closest(pt.longitude, pt.latitude)
            if dist >= MAX_SITE_MODEL_DISTANCE:
                logging.warn('The site parameter associated to %s came from a '
                             'distance of %d km!' % (pt, dist))
            sitecol.append(
                site.Site(pt, param.vs30, param.measured,
                          param.z1pt0, param.z2pt5, param.backarc, i))
        return site.SiteCollection(sitecol)

    # else use the default site params
    return site.SiteCollection.from_points(
        mesh.lons, mesh.lats, mesh.depths, site_ids, oqparam)


def get_gsims(oqparam):
    """
    Return an ordered list of GSIM instances from the gsim name in the
    configuration file or from the gsim logic tree file.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    gsims = list(map(str, get_rlzs_assoc(oqparam).realizations))
    return list(map(valid.gsim, gsims))


def get_rlzs_assoc(oqparam):
    """
    Extract the GSIM realizations from the gsim_logic_tree file, if present,
    or build a single realization from the gsim attribute. It is only defined
    for the scenario calculators.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    if 'gsim_logic_tree' in oqparam.inputs:
        gsim_lt = get_gsim_lt(oqparam, [])
        if len(gsim_lt.values) != 1:
            gsim_file = os.path.join(
                oqparam.base_path, oqparam.inputs['gsim_logic_tree'])
            raise InvalidFile(
                'The gsim logic tree file %s must contain a single tectonic '
                'region type, found %s instead ' % (
                    gsim_file, list(gsim_lt.values)))
        trt = gsim_lt.values
        rlzs = sorted(get_gsim_lt(oqparam, trt))
    else:
        rlzs = [
            logictree.Realization(
                value=(str(oqparam.gsim),), weight=1, lt_path=('',),
                ordinal=0, lt_uid=('@',))]
    return logictree.RlzsAssoc(rlzs)


def get_correl_model(oqparam):
    """
    Return a correlation object. See :mod:`openquake.hazardlib.correlation`
    for more info.
    """
    correl_name = oqparam.ground_motion_correlation_model
    if correl_name is None:  # no correlation model
        return
    correl_model_cls = getattr(correlation, '%sCorrelationModel' % correl_name)
    return correl_model_cls(**oqparam.ground_motion_correlation_params)


def get_rupture(oqparam):
    """
    Returns a hazardlib rupture by reading the `rupture_model` file.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    rup_model = oqparam.inputs['rupture_model']
    rup_node, = read_nodes(rup_model, lambda el: 'Rupture' in el.tag,
                           source.nodefactory['sourceModel'])
    conv = sourceconverter.RuptureConverter(
        oqparam.rupture_mesh_spacing, oqparam.complex_fault_mesh_spacing)
    return conv.convert_node(rup_node)


def get_gsim_lt(oqparam, trts):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param trts:
        a sequence of tectonic region types as strings
    :returns:
        a GsimLogicTree instance obtained by filtering on the provided
        tectonic region types.
    """
    gsim_file = os.path.join(
        oqparam.base_path, oqparam.inputs['gsim_logic_tree'])
    return logictree.GsimLogicTree(gsim_file, trts)


def get_source_model_lt(oqparam):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        a :class:`openquake.commonlib.logictree.SourceModelLogicTree`
        instance
    """
    fname = oqparam.inputs['source_model_logic_tree']
    return logictree.SourceModelLogicTree(
        fname, validate=False, seed=oqparam.random_seed,
        num_samples=oqparam.number_of_logic_tree_samples)


def possibly_gunzip(fname):
    """
    A file can be .gzipped to save space (this happens
    in the debian package); in that case, let's gunzip it.

    :param fname: a file name (not zipped)
    """
    is_gz = os.path.exists(fname) and fname.endswith('.gz')
    there_is_gz = not os.path.exists(fname) and os.path.exists(fname + '.gz')
    if is_gz:
        return writetmp(gzip.open(fname).read())
    elif there_is_gz:
        return writetmp(gzip.open(fname + '.gz').read())
    return fname


def get_source_models(oqparam, source_model_lt, sitecol=None, in_memory=True):
    """
    Build all the source models generated by the logic tree.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param source_model_lt:
        a :class:`openquake.commonlib.logictree.SourceModelLogicTree` instance
    :param in_memory:
        if True, keep in memory the sources, else just collect the TRTs
    :returns:
        an iterator over :class:`openquake.commonlib.source.SourceModel`
        tuples
    """
    converter = sourceconverter.SourceConverter(
        oqparam.investigation_time,
        oqparam.rupture_mesh_spacing,
        oqparam.complex_fault_mesh_spacing,
        oqparam.width_of_mfd_bin,
        oqparam.area_source_discretization)

    # consider only the effective realizations
    rlzs = logictree.get_effective_rlzs(source_model_lt)
    samples_by_lt_path = source_model_lt.samples_by_lt_path()
    for i, rlz in enumerate(rlzs):
        sm = rlz.value  # name of the source model
        smpath = rlz.lt_path
        num_samples = samples_by_lt_path[smpath]
        if num_samples > 1:
            logging.warn('The source path %s was sampled %d times',
                         smpath, num_samples)
        fname = possibly_gunzip(os.path.join(oqparam.base_path, sm))
        if in_memory:
            apply_unc = source_model_lt.make_apply_uncertainties(smpath)
            try:
                trt_models = source.parse_source_model(
                    fname, converter, apply_unc)
            except ValueError as e:
                if str(e) in ('Surface does not conform with Aki & '
                              'Richards convention',
                              'Edges points are not in the right order'):
                    raise InvalidFile('''\
    %s: %s. Probably you are using an obsolete model.
    In that case you can fix the file with the command
    python -m openquake.engine.tools.correct_complex_sources %s
    ''' % (fname, e, fname))
                else:
                    raise
        else:  # just collect the TRT models
            smodel = next(read_nodes(fname, lambda el: 'sourceModel' in el.tag,
                                     source.nodefactory['sourceModel']))
            trt_models = source.TrtModel.collect(smodel)
        trts = [mod.trt for mod in trt_models]
        source_model_lt.tectonic_region_types.update(trts)

        gsim_file = oqparam.inputs.get('gsim_logic_tree')
        if gsim_file:  # check TRTs
            gsim_lt = get_gsim_lt(oqparam, trts)
            for trt_model in trt_models:
                if trt_model.trt not in gsim_lt.values:
                    raise ValueError(
                        "Found in %r a tectonic region type %r inconsistent "
                        "with the ones in %r" % (sm, trt_model.trt, gsim_file))
                trt_model.gsims = gsim_lt.values[trt_model.trt]
        else:
            gsim_lt = logictree.DummyGsimLogicTree()
        weight = rlz.weight / num_samples
        yield source.SourceModel(
            sm, weight, smpath, trt_models, gsim_lt, i, num_samples, None)


def get_composite_source_model(
        oqparam, sitecol=None, SourceProcessor=source.SourceFilterSplitter,
        monitor=DummyMonitor(), no_distribute=parallel.no_distribute(),
        dstore=Fake()):
    """
    Build the source models by splitting the sources. If prefiltering is
    enabled, also reduce the GSIM logic trees in the underlying source models.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param sitecol:
        a :class:`openquake.hazardlib.site.SiteCollection` instance
    :param SourceProcessor:
        the class used to process the sources
    :param monitor:
        a monitor instance
    :param no_distribute:
        used to disable parallel splitting of the sources
    :param dstore:
        a DataStore instance (possibly fake)
    :returns:
        an iterator over :class:`openquake.commonlib.source.SourceModel`
    """
    processor = SourceProcessor(sitecol, oqparam.maximum_distance, monitor)
    source_model_lt = get_source_model_lt(oqparam)
    smodels = []
    trt_id = 0
    for source_model in get_source_models(
            oqparam, source_model_lt, processor.sitecol,
            in_memory=hasattr(processor, 'process')):
        for trt_model in source_model.trt_models:
            trt_model.id = trt_id
            trt_id += 1
        smodels.append(source_model)
    csm = source.CompositeSourceModel(source_model_lt, smodels)
    if sitecol is not None and hasattr(processor, 'process'):
        processor.process(csm, dstore, no_distribute)
        if not csm.get_sources():
            raise RuntimeError('All sources were filtered away')
    csm.count_ruptures()
    return csm


def get_job_info(oqparam, source_models, sitecol):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param source_models:
        a list of :class:`openquake.commonlib.source.SourceModel` tuples
    :param sitecol:
        a :class:`openquake.hazardlib.site.SiteCollection` instance
    :returns:
        a dictionary with same parameters of the computation, in particular
        the input and output weights
    """
    # The input weight is given by the number of ruptures generated
    # by the sources; for point sources however a corrective factor
    # given by the parameter `point_source_weight` is applied
    input_weight = sum((src.weight or 0) * src_model.samples
                       for src_model in source_models
                       for trt_model in src_model.trt_models
                       for src in trt_model)
    imtls = oqparam.imtls
    n_sites = len(sitecol) if sitecol else 0

    # the imtls dictionary has values None when the levels are unknown
    # (this is a valid case for the event based hazard calculator)
    if None in imtls.values():  # there are no levels
        n_imts = len(imtls)
        n_levels = 0
    else:  # there are levels
        n_imts = len(imtls)
        n_levels = sum(len(ls) for ls in imtls.values()) / float(n_imts)

    n_realizations = oqparam.number_of_logic_tree_samples or sum(
        sm.gsim_lt.get_num_paths() for sm in source_models)
    # NB: in the event based case `n_realizations` can be over-estimated,
    # if the method is called in the pre_execute phase, because
    # some tectonic region types may have no occurrencies.

    # The output weight is a pure number which is proportional to the size
    # of the expected output of the calculator. For classical and disagg
    # calculators it is given by
    # n_sites * n_realizations * n_imts * n_levels;
    # for the event based calculator is given by n_sites * n_realizations
    # * n_levels * n_imts * (n_ses * investigation_time) * NORMALIZATION_FACTOR
    output_weight = n_sites * n_imts * n_realizations
    if oqparam.calculation_mode == 'event_based':
        total_time = (oqparam.investigation_time *
                      oqparam.ses_per_logic_tree_path)
        output_weight *= total_time * NORMALIZATION_FACTOR
    else:
        output_weight *= n_levels

    n_sources = 0  # to be set later
    return numpy.array([
        (input_weight, output_weight, n_imts, n_levels, n_sites, n_sources,
         n_realizations)], info_dt)


def get_imts(oqparam):
    """
    Return a sorted list of IMTs as hazardlib objects
    """
    return list(map(imt.from_string, sorted(oqparam.imtls)))


def get_risk_model(oqparam, rmdict):
    """
    Return a :class:`openquake.risklib.riskinput.RiskModel` instance

   :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
   :param rmdict:
        a dictionary (imt, taxonomy) -> loss_type -> risk_function
    """
    wfs = {}  # (imt, taxonomy) -> workflow
    riskmodel = riskinput.RiskModel(wfs)
    if getattr(oqparam, 'limit_states', []):
        # classical_damage/scenario_damage calculator
        if not oqparam.calculation_mode.endswith('_damage'):
            # case when the risk files are in the job_hazard.ini file
            oqparam.calculation_mode += '_damage'
        riskmodel.damage_states = ['no_damage'] + oqparam.limit_states
        delattr(oqparam, 'limit_states')
        for imt_taxo, ffs_by_lt in rmdict.items():
            wfs[imt_taxo] = workflows.get_workflow(
                imt_taxo[0], imt_taxo[1], oqparam,
                fragility_functions=ffs_by_lt)
    elif oqparam.calculation_mode.endswith('_bcr'):
        # classical_bcr calculator
        retro = get_risk_models(oqparam, 'vulnerability_retrofitted')
        for (imt_taxo, vf_orig), (imt_taxo_, vf_retro) in \
                zip(rmdict.items(), retro.items()):
            assert imt_taxo == imt_taxo_  # same imt and taxonomy
            wfs[imt_taxo] = workflows.get_workflow(
                imt_taxo[0], imt_taxo[1], oqparam,
                vulnerability_functions_orig=vf_orig,
                vulnerability_functions_retro=vf_retro)
    else:
        # classical, event based and scenario calculators
        for imt_taxo, vfs in rmdict.items():
            for vf in vfs.values():
                # set the seed; this is important for the case of
                # VulnerabilityFunctionWithPMF
                vf.seed = oqparam.random_seed
            wfs[imt_taxo] = workflows.get_workflow(
                imt_taxo[0], imt_taxo[1], oqparam,
                vulnerability_functions=vfs)

    riskmodel.make_curve_builders(oqparam)
    taxonomies = set()
    for imt_taxo, workflow in wfs.items():
        taxonomies.add(imt_taxo[1])
        workflow.riskmodel = riskmodel
        # save the number of nonzero coefficients of variation
        for vf in workflow.risk_functions.values():
            if hasattr(vf, 'covs') and vf.covs.any():
                riskmodel.covs += 1
    riskmodel.taxonomies = sorted(taxonomies)
    return riskmodel

# ########################### exposure ############################ #

COST_TYPE_SIZE = 21  # using 21 chars since business_interruption has 21 chars
cost_type_dt = numpy.dtype([('name', (bytes, COST_TYPE_SIZE)),
                            ('type', (bytes, COST_TYPE_SIZE)),
                            ('unit', (bytes, COST_TYPE_SIZE))])


class DuplicatedID(Exception):
    """
    Raised when two assets with the same ID are found in an exposure model
    """


def get_exposure_lazy(fname, ok_cost_types):
    """
    :param fname:
        path of the XML file containing the exposure
    :param ok_cost_types:
        a set of cost types (as strings)
    :returns:
        a pair (Exposure instance, list of asset nodes)
    """
    [exposure] = nrml.read_lazy(fname, ['assets'])
    description = exposure.description
    try:
        conversions = exposure.conversions
    except NameError:
        conversions = LiteralNode('conversions',
                                  nodes=[LiteralNode('costTypes', [])])
    try:
        inslimit = conversions.insuranceLimit
    except NameError:
        inslimit = LiteralNode('insuranceLimit', text=True)
    try:
        deductible = conversions.deductible
    except NameError:
        deductible = LiteralNode('deductible', text=True)
    try:
        area = conversions.area
    except NameError:
        area = LiteralNode('area', dict(type=''))

    # read the cost types and make some check
    cost_types = []
    for ct in conversions.costTypes:
        if ct['name'] in ok_cost_types:
            with context(fname, ct):
                cost_types.append(
                    (ct['name'], valid_cost_type(ct['type']), ct['unit']))
    if 'occupants' in ok_cost_types:
        cost_types.append(('occupants', 'per_area', 'people'))
    cost_types.sort(key=operator.itemgetter(0))
    time_events = set()
    return Exposure(
        exposure['id'], exposure['category'],
        ~description, numpy.array(cost_types, cost_type_dt), time_events,
        ~inslimit, ~deductible, area.attrib, [], set()), exposure.assets


valid_cost_type = valid.Choice('aggregated', 'per_area', 'per_asset')


def get_exposure(oqparam):
    """
    Read the full exposure in memory and build a list of
    :class:`openquake.risklib.workflows.Asset` instances.
    If you don't want to keep everything in memory, use
    get_exposure_lazy instead (for experts only).

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        an :class:`Exposure` instance
    """
    out_of_region = 0
    if oqparam.region_constraint:
        region = wkt.loads(oqparam.region_constraint)
    else:
        region = None
    all_cost_types = set(oqparam.all_cost_types)
    fname = oqparam.inputs['exposure']
    exposure, assets_node = get_exposure_lazy(fname, all_cost_types)
    cc = workflows.CostCalculator(
        {}, {}, exposure.deductible_is_absolute,
        exposure.insurance_limit_is_absolute)
    for ct in exposure.cost_types:
        name = ct['name']  # structural, nonstructural, ...
        cc.cost_types[name] = ct['type']  # aggregated, per_asset, per_area
        cc.area_types[name] = exposure.area['type']

    relevant_cost_types = all_cost_types - set(['occupants'])
    asset_refs = set()
    ignore_missing_costs = set(oqparam.ignore_missing_costs)

    for asset in assets_node:
        values = {}
        deductibles = {}
        insurance_limits = {}
        retrofitting_values = {}
        with context(fname, asset):
            asset_id = asset['id'].encode('utf8')
            if asset_id in asset_refs:
                raise DuplicatedID(asset_id)
            asset_refs.add(asset_id)
            taxonomy = asset['taxonomy']
            if 'damage' in oqparam.calculation_mode:
                # calculators of 'damage' kind require the 'number'
                # if it is missing a KeyError is raised
                number = asset.attrib['number']
            else:
                # some calculators ignore the 'number' attribute;
                # if it is missing it is considered 1, since we are going
                # to multiply by it
                try:
                    number = asset['number']
                except KeyError:
                    number = 1
                else:
                    if 'occupants' in all_cost_types:
                        values['fatalities_None'] = number
            location = asset.location['lon'], asset.location['lat']
            if region and not geometry.Point(*location).within(region):
                out_of_region += 1
                continue
        try:
            costs = asset.costs
        except NameError:
            costs = LiteralNode('costs', [])
        try:
            occupancies = asset.occupancies
        except NameError:
            occupancies = LiteralNode('occupancies', [])
        for cost in costs:
            with context(fname, cost):
                cost_type = cost['type']
                if cost_type in relevant_cost_types:
                    values[cost_type] = cost['value']
                    retrovalue = cost.attrib.get('retrofitted')
                    if retrovalue is not None:
                        retrofitting_values[cost_type] = retrovalue
                    if oqparam.insured_losses:
                        deductibles[cost_type] = cost['deductible']
                        insurance_limits[cost_type] = cost['insuranceLimit']

        # check we are not missing a cost type
        missing = relevant_cost_types - set(values)
        if missing and missing <= ignore_missing_costs:
            logging.warn(
                'Ignoring asset %s, missing cost type(s): %s',
                asset_id, ', '.join(missing))
            for cost_type in missing:
                values[cost_type] = None
        elif missing and oqparam.calculation_mode != 'classical_damage':
            # TODO: rewrite the classical_damage to work with multiple
            # loss types, then the special case will disappear
            with context(fname, asset):
                raise ValueError("Invalid Exposure. "
                                 "Missing cost %s for asset %s" % (
                                     missing, asset_id))
        tot_fatalities = 0
        for occupancy in occupancies:
            with context(fname, occupancy):
                exposure.time_events.add(occupancy['period'])
                fatalities = 'fatalities_%s' % occupancy['period']
                values[fatalities] = occupancy['occupants']
                tot_fatalities += values[fatalities]
        if occupancies:  # store average fatalities
            values['fatalities_None'] = tot_fatalities / len(occupancies)
        area = float(asset.attrib.get('area', 1))
        ass = workflows.Asset(
            asset_id, taxonomy, number, location, values, area,
            deductibles, insurance_limits, retrofitting_values, cc)
        exposure.assets.append(ass)
        exposure.taxonomies.add(taxonomy)
    if region:
        logging.info('Read %d assets within the region_constraint '
                     'and discarded %d assets outside the region',
                     len(exposure.assets), out_of_region)
        if len(exposure.assets) == 0:
            raise RuntimeError('Could not find any asset within the region!')
    else:
        logging.info('Read %d assets', len(exposure.assets))

    # sanity check
    values = any(len(ass.values) + ass.number for ass in exposure.assets)
    assert values, 'Could not find any value??'
    return exposure


Exposure = collections.namedtuple(
    'Exposure', ['id', 'category', 'description', 'cost_types', 'time_events',
                 'insurance_limit_is_absolute', 'deductible_is_absolute',
                 'area', 'assets', 'taxonomies'])


def get_specific_assets(oqparam):
    """
    Get the assets from the parameters specific_assets or specific_assets_csv

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    """
    try:
        return set(oqparam.specific_assets)
    except AttributeError:
        if 'specific_assets' not in oqparam.inputs:
            return set()
        return set(open(oqparam.inputs['specific_assets']).read().split())


def get_sitecol_assets(oqparam, exposure):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        two sequences of the same length: the site collection and an
        array with the assets per each site, collected by taxonomy
    """
    assets_by_loc = groupby(exposure.assets, key=lambda a: a.location)
    lons, lats = zip(*sorted(assets_by_loc))
    mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
    sitecol = get_site_collection(oqparam, mesh)
    assets_by_site = []
    for lonlat in zip(sitecol.lons, sitecol.lats):
        assets = assets_by_loc[lonlat]
        assets_by_site.append(sorted(assets, key=operator.attrgetter('id')))
    return sitecol, numpy.array(assets_by_site)


def get_mesh_csvdata(csvfile, imts, num_values, validvalues):
    """
    Read CSV data in the format `IMT lon lat value1 ... valueN`.

    :param csvfile:
        a file or file-like object with the CSV data
    :param imts:
        a list of intensity measure types
    :param num_values:
        dictionary with the number of expected values per IMT
    :param validvalues:
        validation function for the values
    :returns:
        the mesh of points and the data as a dictionary
        imt -> array of curves for each site
    """
    number_of_values = dict(zip(imts, num_values))
    lon_lats = {imt: set() for imt in imts}
    data = AccumDict()  # imt -> list of arrays
    check_imt = valid.Choice(*imts)
    for line, row in enumerate(csv.reader(csvfile, delimiter=' '), 1):
        try:
            imt = check_imt(row[0])
            lon_lat = valid.longitude(row[1]), valid.latitude(row[2])
            if lon_lat in lon_lats[imt]:
                raise DuplicatedPoint(lon_lat)
            lon_lats[imt].add(lon_lat)
            values = validvalues(' '.join(row[3:]))
            if len(values) != number_of_values[imt]:
                raise ValueError('Found %d values, expected %d' %
                                 (len(values), number_of_values[imt]))
        except (ValueError, DuplicatedPoint) as err:
            raise err.__class__('%s: file %s, line %d' % (err, csvfile, line))
        data += {imt: [numpy.array(values)]}
    points = lon_lats.pop(imts[0])
    for other_imt, other_points in lon_lats.items():
        if points != other_points:
            raise ValueError('Inconsistent locations between %s and %s' %
                             (imts[0], other_imt))
    lons, lats = zip(*sorted(points))
    mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
    return mesh, {imt: numpy.array(lst) for imt, lst in data.items()}


def get_gmfs(oqparam):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        sitecol, tags, gmf array
    """
    fname = oqparam.inputs['gmfs']
    if fname.endswith('.txt'):
        return get_gmfs_from_txt(oqparam, fname)
    elif fname.endswith('.xml'):
        return get_scenario_from_nrml(oqparam, fname)
    else:
        raise NotImplemented('Reading from %s' % fname)


def get_hcurves(oqparam):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        sitecol, imtls, curve array
    """
    fname = oqparam.inputs['hazard_curves']
    if fname.endswith('.csv'):
        return get_hcurves_from_csv(oqparam, fname)
    elif fname.endswith('.xml'):
        return get_hcurves_from_xml(oqparam, fname)
    else:
        raise NotImplemented('Reading from %s' % fname)


def get_hcurves_from_csv(oqparam, fname):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param fname:
        a .txt file with format `IMT lon lat poe1 ... poeN`
    :returns:
        the site collection and the hazard curves read by the .txt file
    """
    if not oqparam.imtls:
        oqparam.set_risk_imtls(get_risk_models(oqparam))
    if not oqparam.imtls:
        raise ValueError('Missing intensity_measure_types_and_levels in %s'
                         % oqparam.inputs['job_ini'])
    num_values = list(map(len, list(oqparam.imtls.values())))
    with open(oqparam.inputs['hazard_curves']) as csvfile:
        mesh, hcurves_by_imt = get_mesh_csvdata(
            csvfile, list(oqparam.imtls), num_values,
            valid.decreasing_probabilities)
    sitecol = get_site_collection(oqparam, mesh)
    return sitecol, hcurves_by_imt


def get_hcurves_from_xml(oqparam, fname):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param fname:
        an XML file containing hazard curves
    :returns:
        sitecol, curve array
    """
    hcurves_by_imt = {}
    oqparam.hazard_imtls = imtls = {}
    for hcurves in nrml.read(fname):
        imt = hcurves['IMT']
        if imt == 'SA':
            imt += '(%s)' % hcurves['saPeriod']
        imtls[imt] = ~hcurves.IMLs
        data = []
        for node in hcurves[1:]:
            xy = ~node.Point.pos
            poes = ~node.poEs
            data.append((xy, poes))
        data.sort()
        hcurves_by_imt[imt] = numpy.array([d[1] for d in data])
    n = len(hcurves_by_imt[imt])
    curves = zero_curves(n, imtls)
    for imt in imtls:
        curves[imt] = hcurves_by_imt[imt]
    lons, lats = [], []
    for xy, poes in data:
        lons.append(xy[0])
        lats.append(xy[1])
    mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
    sitecol = get_site_collection(oqparam, mesh)
    return sitecol, curves


def get_gmfs_from_txt(oqparam, fname):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param fname:
        the full path of the CSV file
    :returns:
        a composite array of shape (N, R) read from a CSV file with format
        `tag indices [gmv1 ... gmvN] * num_imts`
    """
    with open(fname) as csvfile:
        firstline = next(csvfile)
        try:
            coords = valid.coordinates(firstline)
        except:
            raise InvalidFile(
                'The first line of %s is expected to contain comma separated'
                'ordered coordinates, got %s instead' % (fname, firstline))
        sitecol = sitecol_from_coords(oqparam, coords)
        if not oqparam.imtls:
            oqparam.set_risk_imtls(get_risk_models(oqparam))
        imts = list(oqparam.imtls)
        imt_dt = numpy.dtype([(imt, float) for imt in imts])
        num_gmfs = oqparam.number_of_ground_motion_fields
        gmf_by_imt = numpy.zeros((num_gmfs, len(sitecol)), imt_dt)
        tags = []

        for lineno, line in enumerate(csvfile, 2):
            row = line.split(',')
            try:
                indices = list(map(valid.positiveint, row[1].split()))
            except:
                raise InvalidFile(
                    'The second column in %s is expected to contain integer '
                    'indices, got %s' % (fname, row[1]))
            r_sites = (
                sitecol if not indices else
                site.FilteredSiteCollection(indices, sitecol))
            for i in range(len(imts)):
                try:
                    array = numpy.array(valid.positivefloats(row[i + 2]))
                    # NB: i + 2 because the first 2 fields are tag and indices
                except:
                    raise InvalidFile(
                        'The column #%d in %s is expected to contain positive '
                        'floats, got %s instead' % (i + 3, fname, row[i + 2]))
                gmf_by_imt[imts[i]][lineno - 2] = r_sites.expand(array, 0)
            tags.append(row[0])
    if lineno < num_gmfs + 1:
        raise InvalidFile('%s contains %d rows, expected %d' % (
            fname, lineno, num_gmfs + 1))
    if tags != sorted(tags):
        raise InvalidFile('The tags in %s are not ordered: %s' % (fname, tags))
    return sitecol, numpy.array(tags, '|S100'), gmf_by_imt.T


# used in get_scenario_from_nrml
def _extract_tags_sites(gmfset):
    tags = set()
    mesh = set()
    for gmf in gmfset:
        tags.add(gmf['ruptureId'])
        for node in gmf:
            mesh.add((node['lon'], node['lat']))
    return numpy.array(sorted(tags), '|S100'), sorted(mesh)


def get_scenario_from_nrml(oqparam, fname):
    """
    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :param fname:
        the NRML files containing the GMFs
    :returns:
        a triple (sitecol, rupture_tags, gmf array)
    """
    if not oqparam.imtls:
        oqparam.set_risk_imtls(get_risk_models(oqparam))
    imts = list(oqparam.imtls)
    imt_dt = numpy.dtype([(imt, float) for imt in imts])
    gmfset = nrml.read(fname).gmfCollection.gmfSet
    tags, oqparam.sites = _extract_tags_sites(gmfset)
    oqparam.number_of_ground_motion_fields = num_events = len(tags)
    sitecol = get_site_collection(oqparam)
    num_sites = len(oqparam.sites)
    gmf_by_imt = numpy.zeros((num_events, num_sites), imt_dt)
    num_imts = len(imts)
    counts = collections.Counter()
    for i, gmf in enumerate(gmfset):
        if len(gmf) != num_sites:  # there must be one node per site
            raise InvalidFile('Expected %d sites, got %d in %s, line %d' % (
                num_sites, len(gmf), fname, gmf.lineno))
        counts[gmf['ruptureId']] += 1
        imt = gmf['IMT']
        if imt == 'SA':
            imt = 'SA(%s)' % gmf['saPeriod']
        for site_idx, lon, lat, node in zip(
                range(num_sites), sitecol.lons, sitecol.lats, gmf):
            if (node['lon'], node['lat']) != (lon, lat):
                raise InvalidFile('The site mesh is not ordered in %s, line %d'
                                  % (fname, node.lineno))
            try:
                gmf_by_imt[imt][i % num_events, site_idx] = node['gmv']
            except IndexError:
                raise InvalidFile('Something wrong in %s, line %d' %
                                  (fname, node.lineno))
    for tag, count in counts.items():
        if count < num_imts:
            raise InvalidFile('Found a missing tag %r in %s' %
                              (tag, fname))
        elif count > num_imts:
            raise InvalidFile('Found a duplicated tag %r in %s' %
                              (tag, fname))
    return sitecol, tags, gmf_by_imt.T


def get_mesh_hcurves(oqparam):
    """
    Read CSV data in the format `lon lat, v1-vN, w1-wN, ...`.

    :param oqparam:
        an :class:`openquake.commonlib.oqvalidation.OqParam` instance
    :returns:
        the mesh of points and the data as a dictionary
        imt -> array of curves for each site
    """
    imtls = oqparam.imtls
    lon_lats = set()
    data = AccumDict()  # imt -> list of arrays
    ncols = len(imtls) + 1  # lon_lat + curve_per_imt ...
    csvfile = oqparam.inputs['hazard_curves']
    for line, row in enumerate(csv.reader(csvfile), 1):
        try:
            if len(row) != ncols:
                raise ValueError('Expected %d columns, found %d' %
                                 ncols, len(row))
            x, y = row[0].split()
            lon_lat = valid.longitude(x), valid.latitude(y)
            if lon_lat in lon_lats:
                raise DuplicatedPoint(lon_lat)
            lon_lats.add(lon_lat)
            for i, imt_ in enumerate(imtls, 1):
                values = valid.decreasing_probabilities(row[i])
                if len(values) != len(imtls[imt_]):
                    raise ValueError('Found %d values, expected %d' %
                                     (len(values), len(imtls([imt_]))))
                data += {imt_: [numpy.array(values)]}
        except (ValueError, DuplicatedPoint) as err:
            raise err.__class__('%s: file %s, line %d' % (err, csvfile, line))
    lons, lats = zip(*sorted(lon_lats))
    mesh = geo.Mesh(numpy.array(lons), numpy.array(lats))
    return mesh, {imt: numpy.array(lst) for imt, lst in data.items()}
