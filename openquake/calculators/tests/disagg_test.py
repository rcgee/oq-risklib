import unittest
from nose.plugins.attrib import attr
from openquake.calculators.tests import CalculatorTestCase
from openquake.qa_tests_data.disagg import case_1, case_2

try:
    from shapely.geos import geos_version
except:
    old_geos = True
else:
    old_geos = geos_version < (3, 4, 2)


class DisaggregationTestCase(CalculatorTestCase):

    def assert_curves_ok(self, expected, test_dir, delta=None):
        if old_geos:  # Ubuntu 12.04
            raise unittest.SkipTest('libgeos too old')
        out = self.run_calc(test_dir, 'job.ini', exports='xml')
        got = out['disagg', 'xml']
        self.assertEqual(len(expected), len(got))
        for fname, actual in zip(expected, got):
            self.assertEqualFiles(
                'expected_output/%s' % fname, actual)

    @attr('qa', 'hazard', 'classical')
    def test_case_1(self):
        self.assert_curves_ok([
            'poe-0.02-rlz-0-PGA-10.1-40.1.xml',
            'poe-0.02-rlz-0-SA(0.025)-10.1-40.1.xml',
            'poe-0.1-rlz-0-PGA-10.1-40.1.xml',
            'poe-0.1-rlz-0-SA(0.025)-10.1-40.1.xml'], case_1.__file__)

    @attr('qa', 'hazard', 'classical')
    def test_case_2(self):
        self.assert_curves_ok(
            ['poe-0.02-rlz-0-PGA--3.0--3.0.xml',
             'poe-0.02-rlz-0-PGA-0.0-0.0.xml',
             'poe-0.02-rlz-1-PGA--3.0--3.0.xml',
             'poe-0.02-rlz-1-PGA-0.0-0.0.xml',
             'poe-0.02-rlz-2-PGA-0.0-0.0.xml',
             'poe-0.02-rlz-3-PGA-0.0-0.0.xml',
             'poe-0.1-rlz-0-PGA--3.0--3.0.xml',
             'poe-0.1-rlz-0-PGA-0.0-0.0.xml',
             'poe-0.1-rlz-1-PGA--3.0--3.0.xml',
             'poe-0.1-rlz-1-PGA-0.0-0.0.xml',
             'poe-0.1-rlz-2-PGA-0.0-0.0.xml',
             'poe-0.1-rlz-3-PGA-0.0-0.0.xml'],
            case_2.__file__)
