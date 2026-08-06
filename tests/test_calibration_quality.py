import math
import unittest

import calibration_quality


class CalibrationQualityTests(unittest.TestCase):
    def test_clean_origin_fit_passes(self):
        result = calibration_quality.assess_origin_fit(
            [0, 10, 20, 30, 40],
            [0, 5, 10, 15, 20],
            [0, 1, 2, 3, 4],
            coefficient_min=0.05,
            coefficient_max=100.0,
        )
        self.assertTrue(result["passed"], result["reasons"])
        self.assertAlmostEqual(result["coefficient"], 0.5)
        self.assertAlmostEqual(result["free_intercept"], 0.0)

    def test_free_intercept_diagnostic_rejects_offset_fit(self):
        result = calibration_quality.assess_origin_fit(
            [0, 10, 20, 30, 40],
            [10, 15, 20, 25, 30],
            [0, 1, 2, 3, 4],
            coefficient_min=0.05,
            coefficient_max=100.0,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("intercept" in reason for reason in result["reasons"]))

    def test_non_finite_and_negative_data_fail_closed(self):
        result = calibration_quality.assess_origin_fit(
            [0, math.nan, 2],
            [0, -1, 2],
            [0, 1, 2],
            coefficient_min=0.05,
            coefficient_max=100.0,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("finite" in reason for reason in result["reasons"]))
        self.assertTrue(any("non-negative" in reason for reason in result["reasons"]))

    def test_non_monotonic_sweep_is_rejected(self):
        result = calibration_quality.assess_origin_fit(
            [0, 10, 30, 20, 40],
            [0, 5, 15, 10, 20],
            [0, 1, 2, 3, 4],
            coefficient_min=0.05,
            coefficient_max=100.0,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("monotonic" in reason for reason in result["reasons"]))

    def test_coefficient_range_is_enforced(self):
        result = calibration_quality.assess_origin_fit(
            [0, 1, 2, 3],
            [0, 1000, 2000, 3000],
            [0, 1, 2, 3],
            coefficient_min=0.05,
            coefficient_max=100.0,
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("coefficient" in reason for reason in result["reasons"]))


if __name__ == "__main__":
    unittest.main()
