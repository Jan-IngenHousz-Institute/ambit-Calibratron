"""Tests for the LED and ADPD gates, including the four defects they fix.

The repo-root ``calibration_quality.py`` is still correct arithmetic; these tests
pin the four behaviours that were wrong *for this bench* and are now right.
"""

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quality


def led_sweep(coefficient=0.2, settings=(0, 10, 20, 60, 90, 150, 250)):
    """A clean sweep: measured PAR = setting / coefficient, no offset."""
    measured = [s / coefficient for s in settings]
    return measured, list(settings)


# ============================================================================
# Defect 1: the firmware's low bound is EXCLUSIVE
# ============================================================================

def test_actinic_predicate_matches_the_firmware_polarity():
    # ambit/test/calibration/test_main.cpp asserts "actinic lower bound is
    # exclusive" and "actinic upper bound is inclusive". Mirror both exactly.
    assert not quality.valid_actinic_coefficient(0.01)      # exclusive
    assert quality.valid_actinic_coefficient(0.0101)
    assert quality.valid_actinic_coefficient(1.0)           # inclusive
    assert not quality.valid_actinic_coefficient(1.0001)
    assert not quality.valid_actinic_coefficient(float("nan"))
    assert not quality.valid_actinic_coefficient(float("inf"))


def test_a_coefficient_of_exactly_the_low_bound_is_rejected_host_side():
    # The old gate was `coefficient_min <= coefficient`, so exactly 0.01 passed
    # host QC and was then refused by the device - a write the bench would report
    # as successful while nothing changed.
    measured, settings = led_sweep(coefficient=0.01)
    result = quality.assess_led_fit(measured, settings)
    assert result["coefficient"] == pytest.approx(0.01)
    assert not result["passed"]
    assert any("exclusive" in r for r in result["reasons"])

    # Just inside the bound is fine.
    measured, settings = led_sweep(coefficient=0.0101)
    assert quality.assess_led_fit(measured, settings)["passed"]


def test_a_coefficient_above_the_high_bound_is_rejected():
    measured, settings = led_sweep(coefficient=1.5)
    result = quality.assess_led_fit(measured, settings)
    assert not result["passed"]
    assert any("act_led_coeff" in r for r in result["reasons"])


# ============================================================================
# Defect 2: the dead stimulus check
# ============================================================================

def test_monotonicity_is_checked_on_the_measurement_not_the_stimulus():
    # The old API took (x, y, stimulus) and the LED call site passed the settings
    # as both y and stimulus, so the "reference readings are not monotonic"
    # branch compared settings to themselves and could never fire. With two
    # arguments the only meaningful direction is the one that is checked.
    measured, settings = led_sweep()
    measured[3], measured[5] = measured[5], measured[3]      # detector misbehaves
    result = quality.assess_led_fit(measured, settings)
    assert not result["passed"]
    assert any("monotonic" in r for r in result["reasons"])


def test_the_sweep_need_not_be_supplied_in_stimulus_order():
    # Ordering is derived from the settings, so a shuffled sweep still passes.
    measured, settings = led_sweep()
    order = [4, 0, 6, 2, 1, 5, 3]
    shuffled_m = [measured[i] for i in order]
    shuffled_s = [settings[i] for i in order]
    assert quality.assess_led_fit(shuffled_m, shuffled_s)["passed"]


# ============================================================================
# Defect 3: a slightly negative dark reading must not fail the sweep
# ============================================================================

def test_a_slightly_negative_dark_reading_is_clamped_not_fatal():
    # The MiniPAR reports par_raw * slope + intercept; a negative intercept puts
    # a genuinely dark reading a shade below zero. That is noise, not bad data,
    # and the old gate failed the entire calibration on it.
    measured, settings = led_sweep()
    measured[0] = -0.8                       # dark point, setting 0
    result = quality.assess_led_fit(measured, settings)
    assert result["passed"], result["reasons"]
    assert any("clamped" in n for n in result["notes"])


def test_a_large_negative_reading_is_still_fatal():
    measured, settings = led_sweep()
    measured[0] = -400.0
    result = quality.assess_led_fit(measured, settings)
    assert not result["passed"]
    assert any("negative beyond the dark tolerance" in r for r in result["reasons"])


def test_negative_settings_are_rejected():
    measured, settings = led_sweep()
    settings[1] = -10
    assert not quality.assess_led_fit(measured, settings)["passed"]


# ============================================================================
# Defect 4: thresholds must not collide with the tier-3 ones
# ============================================================================

def test_thresholds_are_named_for_their_step():
    # spec_cal defines MIN_R2/MAX_NRMSE at different values for the affine fit.
    # Same names in two modules is how a tuning edit lands in the wrong gate.
    import spec_cal

    assert quality.LED_MIN_R2 == 0.99
    assert spec_cal.MIN_R2 == 0.995                  # deliberately different
    assert not hasattr(quality, "MIN_R2")
    assert not hasattr(quality, "MAX_NRMSE")

    reported = quality.assess_led_fit(*led_sweep())["thresholds"]
    assert reported["min_r2"] == quality.LED_MIN_R2
    assert reported["coefficient_min_exclusive"] == quality.ACTINIC_COEFFICIENT_MIN


# ============================================================================
# The origin model itself - which IS right for the LED
# ============================================================================

def test_clean_origin_fit_passes_and_recovers_the_coefficient():
    measured, settings = led_sweep(coefficient=0.2)
    result = quality.assess_led_fit(measured, settings)
    assert result["passed"], result["reasons"]
    assert result["coefficient"] == pytest.approx(0.2)
    assert result["r2"] == pytest.approx(1.0)
    assert result["free_intercept"] == pytest.approx(0.0, abs=1e-9)
    assert result["fit"] == "through_origin"


def test_a_real_intercept_is_evidence_against_the_origin_model():
    # Unlike tier 3, an intercept here means the model is wrong - an LED turn-on
    # threshold, or a light leak onto the reference - so it stays a rejection.
    measured, settings = led_sweep()
    settings = [s + 25 for s in settings]
    result = quality.assess_led_fit(measured, settings)
    assert not result["passed"]
    assert any("origin model does not hold" in r for r in result["reasons"])


def test_nonlinear_sweep_is_rejected():
    measured, settings = led_sweep()
    measured[4] *= 1.5
    result = quality.assess_led_fit(measured, settings)
    assert not result["passed"]


def test_degenerate_inputs_fail_closed():
    assert not quality.assess_led_fit([0.0] * 6, [0] * 6)["passed"]
    assert not quality.assess_led_fit([1.0, 2.0], [10, 20])["passed"]        # too few
    nan = quality.assess_led_fit([0.0, math.nan, 2.0, 3.0], [0, 1, 2, 3])
    assert not nan["passed"]
    assert any("finite" in r for r in nan["reasons"])
    with pytest.raises(ValueError):
        quality.assess_led_fit([1.0, 2.0, 3.0], [1, 2])


# ============================================================================
# ADPD dark baseline
# ============================================================================

def test_a_good_baseline_passes():
    gate = quality.assess_adpd_baseline([120, 300, 300, 300, 300, 300],
                                        [130, 310, 310, 310, 310, 310])
    assert gate["passed"], gate["reasons"]


def test_s630_over_the_firmware_limit_is_rejected():
    gate = quality.assess_adpd_baseline([401, 300, 300, 300, 300, 300],
                                        [130, 310, 310, 310, 310, 310])
    assert not gate["passed"]
    assert any("dark fixture" in r for r in gate["reasons"])
    # 400 exactly is the inclusive firmware limit.
    assert quality.assess_adpd_baseline([400, 300, 300, 300, 300, 300],
                                        [130, 310, 310, 310, 310, 310])["passed"]


def test_an_unreadable_previous_baseline_is_fatal_because_rollback_needs_it():
    gate = quality.assess_adpd_baseline([120, 300, 300, 300, 300, 300], [])
    assert not gate["passed"]
    assert any("rollback" in r for r in gate["reasons"])


def test_out_of_range_and_wrong_length_measurements_are_rejected():
    good_prev = [130, 310, 310, 310, 310, 310]
    assert not quality.assess_adpd_baseline([120, 300, 300], good_prev)["passed"]
    assert not quality.assess_adpd_baseline(
        [120, 300, 300, 300, 300, 0x1000000], good_prev)["passed"]
    assert not quality.assess_adpd_baseline(
        [120, 300, 300, 300, 300, -1], good_prev)["passed"]
    # bools are ints in Python; a True in a baseline is a bug, not a 1.
    assert not quality.assess_adpd_baseline(
        [120, 300, 300, 300, 300, True], good_prev)["passed"]


def test_adpd_predicate_mirrors_the_firmware():
    assert quality.valid_adpd_baseline([400, 0, 0, 0, 0, quality.MAX_ADPD_BASELINE])
    assert not quality.valid_adpd_baseline([401, 0, 0, 0, 0, 0])
    assert not quality.valid_adpd_baseline([0, 0, 0, 0, 0])


# ============================================================================
# assess_adpd_sweep: which channel is supposed to respond
# ============================================================================

def _flat(n=5, mean=660.0):
    return {"n": n, "mean": mean, "std": 2.0, "min": mean - 3, "max": mean + 3,
            "saturated": False}


def _ramp(mean):
    return {"n": 5, "mean": mean, "std": 2.0, "min": mean - 3, "max": mean + 3,
            "saturated": False}


def test_a_channel_aimed_elsewhere_is_not_reported_as_a_fault():
    # LED sweep geometry: leaf faces the actinic LED, sun does not. Real numbers
    # from a bench run - sun wanders 651..656 with no relation to the drive.
    summaries = [{"leaf": _ramp(m), "sun": _flat(mean=s)}
                 for m, s in zip([798, 810, 860, 902, 988, 1134],
                                 [651.0, 653.2, 652.6, 655.2, 655.8, 656.4])]
    report = quality.assess_adpd_sweep(summaries, [10, 20, 60, 90, 150, 250],
                                       responders=("leaf",))
    assert report["leaf"]["notes"] == []
    assert report["sun"]["notes"] == []                  # reported, not flagged
    assert report["sun"]["expected_to_respond"] is False
    assert report["sun"]["span"] > 0                     # the numbers still travel


def test_the_facing_channel_is_still_held_to_account():
    summaries = [{"leaf": _flat(mean=800.0)} for _ in range(5)]
    report = quality.assess_adpd_sweep(summaries, [10, 20, 60, 90, 150],
                                       channels=("leaf",), responders=("leaf",))
    assert "no response across the sweep" in report["leaf"]["notes"][0]


def test_saturation_is_flagged_wherever_the_channel_points():
    pinned = dict(_flat(), saturated=True)
    summaries = [{"sun": pinned}, {"sun": pinned}]
    report = quality.assess_adpd_sweep(summaries, [10, 20], channels=("sun",),
                                       responders=("leaf",))
    assert any("pinned at full scale" in n for n in report["sun"]["notes"])


def test_default_still_expects_every_named_channel_to_respond():
    summaries = [{"leaf": _flat(mean=800.0)} for _ in range(4)]
    report = quality.assess_adpd_sweep(summaries, [1, 2, 3, 4], channels=("leaf",))
    assert report["leaf"]["expected_to_respond"] is True
    assert report["leaf"]["notes"]
