"""Tests for the pure cmd-35 host layer.

Everything here runs without hardware. The codecs are tested against bytes built
the way the firmware builds them (src/run_esp.cpp), and the tier math against
the plan's own anchors - 139 ms at ATIME 99 / ASTEP 499, and the gain ordinals it
calls out explicitly.
"""

import math
import os
import struct
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import spec_cal


# ============================================================================
# Fixtures: build wire payloads the way the firmware would
# ============================================================================

def make_spec_raw(*, fmt=1, atime=99, gain_low=2, gain_high=2, astep=499,
                  flags=0, sat_mask=0, clip_mask=0, raw=None, chan=None,
                  par=0.0, par_tier2=0.0):
    raw = list(raw if raw is not None else [1000] * 10)
    chan = list(chan if chan is not None else [0.0] * 10)
    return struct.pack("<BBBBHHHH10H10fff", fmt, atime, gain_low, gain_high,
                       astep, flags, sat_mask, clip_mask, *raw, *chan,
                       par, par_tier2)


def make_spec_cal(*, fmt=1, spec_offset=None, spec_sens=None, par_weight=None,
                  par_slope=1.0, par_intercept=0.0):
    spec_offset = list(spec_offset if spec_offset is not None else spec_cal.SEED_SPEC_OFFSET)
    spec_sens = list(spec_sens if spec_sens is not None else spec_cal.SEED_SPEC_SENS)
    par_weight = list(par_weight if par_weight is not None else spec_cal.SEED_PAR_WEIGHT)
    return struct.pack("<BBH30fff", fmt, 0, 0,
                       *spec_offset, *spec_sens, *par_weight,
                       par_slope, par_intercept)


def consistent_pair(**raw_kwargs):
    """A (SpecRaw, SpecCal) pair whose derived fields the firmware got right.

    Built by running the host chain forwards and stuffing the results into the
    payload, which is exactly what a correct firmware does.
    """
    cal = spec_cal.decode_spec_cal(make_spec_cal(par_slope=1.25, par_intercept=7.983))
    probe = spec_cal.decode_spec_raw(make_spec_raw(**raw_kwargs))
    got = spec_cal.recompute(probe, cal)
    reading = spec_cal.decode_spec_raw(make_spec_raw(
        chan=got.chan, par=got.par, par_tier2=got.par_tier2,
        clip_mask=got.clip_mask, sat_mask=got.sat_mask,
        **raw_kwargs))
    return reading, cal


def spec_cal_text(par_slope=1.25, par_intercept=7.983, **overrides):
    """The five labelled lines get_spec_cal prints, at the firmware's %.9g."""
    fields = {"spec_offset": spec_cal.SEED_SPEC_OFFSET,
              "spec_sens": spec_cal.SEED_SPEC_SENS,
              "par_weight": spec_cal.SEED_PAR_WEIGHT}
    fields.update(overrides)
    lines = [name + ":" + ",".join(format(v, ".9g") for v in fields[name])
             for name in ("spec_offset", "spec_sens", "par_weight")]
    lines.append("par_slope:" + format(par_slope, ".9g"))
    lines.append("par_intercept:" + format(par_intercept, ".9g"))
    return (chr(13) + chr(10)).join(lines)          # the device sends CRLF


# ============================================================================
# Tier 1
# ============================================================================

def test_gain_multiplier_matches_the_ordinals_the_plan_calls_out():
    # Plan section 8: ordinal 0 is 0.5x - dividing by the raw byte divides by
    # zero - and 3/4/10 mean 4x/8x/512x. Ordinal 2 = 2x is the only fixed point,
    # and it is the pinned value, which is why an ordinal bug is invisible today.
    assert spec_cal.gain_multiplier(0) == 0.5
    assert spec_cal.gain_multiplier(2) == 2.0
    assert spec_cal.gain_multiplier(3) == 4.0
    assert spec_cal.gain_multiplier(4) == 8.0
    assert spec_cal.gain_multiplier(10) == 512.0


def test_gain_multiplier_rejects_out_of_range_ordinals():
    with pytest.raises(ValueError):
        spec_cal.gain_multiplier(11)
    with pytest.raises(ValueError):
        spec_cal.gain_multiplier(-1)


def test_integration_time_hits_the_plans_139ms_anchor():
    # Plan decision 3 / section 8: ATIME 99 / ASTEP 499 must give 139 ms. If this
    # comes out at 0.139 someone reintroduced the seconds tick.
    assert spec_cal.integration_time_ms(99, 499) == pytest.approx(139.0, rel=1e-9)


def test_full_scale_is_computed_from_the_reported_exposure():
    assert spec_cal.full_scale_counts(99, 499) == 50000
    # Capped at the 16-bit counter, which the firmware's own macro omits because
    # at the pinned exposure the cap never binds.
    assert spec_cal.full_scale_counts(255, 511) == 0xFFFF


def test_basic_counts_split_the_banks_at_slot_four():
    # F1-F4 divide by gain_low; F5-F8 AND NIR AND Clear divide by gain_high
    # (plan section 8 - three comments in the ambit repo said otherwise).
    x = spec_cal.basic_counts([278] * 10, gain_low=2, gain_high=3,
                              atime=99, astep=499)
    assert x[0:4] == pytest.approx([1.0] * 4)          # 278 / (2 * 139)
    assert x[4:10] == pytest.approx([0.5] * 6)         # 278 / (4 * 139)


# ============================================================================
# Codecs
# ============================================================================

def test_struct_sizes_match_the_documented_layouts():
    assert len(make_spec_raw()) == spec_cal.SPEC_RAW_SIZE == 80
    assert len(make_spec_cal()) == spec_cal.SPEC_CAL_SIZE == 132


def test_decode_spec_raw_lands_every_field_at_its_documented_offset():
    reading = spec_cal.decode_spec_raw(make_spec_raw(
        atime=99, gain_low=2, gain_high=3, astep=499,
        flags=(spec_cal.FLAG_SATURATED | spec_cal.FLAG_FAULT
               | spec_cal.FLAG_TIER3_STORED),
        sat_mask=0b0000000101, clip_mask=0b1000000000,
        raw=list(range(10, 110, 10)), chan=[float(i) for i in range(10)],
        par=123.5, par_tier2=99.25))
    assert reading.format == 1
    assert (reading.atime, reading.astep) == (99, 499)
    assert (reading.gain_low, reading.gain_high) == (2, 3)
    assert reading.raw == [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert reading.chan == pytest.approx([float(i) for i in range(10)])
    assert reading.par == pytest.approx(123.5)
    assert reading.par_tier2 == pytest.approx(99.25)
    assert reading.tint_ms == pytest.approx(139.0)
    assert reading.masked_channels(reading.sat_mask) == ["f1_415", "f3_480"]
    assert reading.masked_channels(reading.clip_mask) == ["clear"]
    assert reading.fatal_flags == (spec_cal.FLAG_SATURATED | spec_cal.FLAG_FAULT)
    assert reading.tier3_stored is True
    assert reading.par_weight_is_fleet_fit is False


def test_the_flags_word_is_zoned_by_polarity():
    # The firmware splits flags into a negative-polarity condition byte and a
    # positive-polarity calibration byte, so an all-zero word - what a truncated
    # or zeroed frame produces - reads as "no fault, nothing confirmed".
    empty = spec_cal.decode_spec_raw(make_spec_raw(flags=0))
    assert empty.fatal_flags == 0
    assert empty.par_weight_is_fleet_fit is False
    assert empty.tier3_stored is False
    assert empty.par_provisional is True          # pessimistic, as intended

    # Confirmation requires BOTH high bits; one alone is not enough.
    only_fleet = spec_cal.decode_spec_raw(
        make_spec_raw(flags=spec_cal.FLAG_PAR_WEIGHT_IS_FLEET_FIT))
    only_tier3 = spec_cal.decode_spec_raw(
        make_spec_raw(flags=spec_cal.FLAG_TIER3_STORED))
    both = spec_cal.decode_spec_raw(make_spec_raw(
        flags=spec_cal.FLAG_PAR_WEIGHT_IS_FLEET_FIT | spec_cal.FLAG_TIER3_STORED))
    assert only_fleet.par_provisional is True
    assert only_tier3.par_provisional is True
    assert both.par_provisional is False

    # Separate bytes, so neither zone can be read as the other.
    assert spec_cal.FATAL_FLAGS & spec_cal.CALIBRATION_FLAGS == 0
    assert (spec_cal.FLAG_PAR_WEIGHT_IS_FLEET_FIT
            | spec_cal.FLAG_TIER3_STORED) & spec_cal.CONDITION_FLAGS == 0


def test_legacy_32_byte_payload_is_named_not_misparsed():
    with pytest.raises(ValueError, match="legacy 32-byte payload"):
        spec_cal.decode_spec_raw(b"\x00" * 32)


def test_wrong_length_and_wrong_format_both_raise():
    with pytest.raises(ValueError, match="80 bytes"):
        spec_cal.decode_spec_raw(b"\x00" * 79)
    with pytest.raises(ValueError, match="format 2"):
        spec_cal.decode_spec_raw(make_spec_raw(fmt=2))


def test_decode_spec_cal_round_trips_the_shipped_seeds():
    cal = spec_cal.decode_spec_cal(make_spec_cal(par_slope=1.0, par_intercept=0.0))
    assert cal.spec_offset == pytest.approx(list(spec_cal.SEED_SPEC_OFFSET), rel=1e-6)
    assert cal.spec_sens == pytest.approx(list(spec_cal.SEED_SPEC_SENS), rel=1e-6)
    assert cal.par_weight == pytest.approx(list(spec_cal.SEED_PAR_WEIGHT), rel=1e-6)
    assert cal.tier3_is_identity()
    assert cal.seed_match() == {"spec_offset": True, "spec_sens": True, "par_weight": True}


def test_text_mirror_parses_the_five_labelled_lines():
    cal = spec_cal.parse_spec_cal_text(spec_cal_text())
    assert cal.source == "text"
    assert cal.par_slope == pytest.approx(1.25)
    assert cal.par_intercept == pytest.approx(7.983)
    assert cal.seed_match() == {"spec_offset": True, "spec_sens": True,
                                "par_weight": True}


def test_text_mirror_keys_on_labels_not_position():
    # A stray log line between the fields must not shift a vector by one field -
    # which, for these vectors, would be invisible in the numbers.
    noisy = spec_cal_text().splitlines()
    noisy.insert(2, "[I][spec] AS7341 ready")
    noisy.insert(0, "some banner without a colon")
    cal = spec_cal.parse_spec_cal_text(noisy)
    assert cal.par_weight == pytest.approx(list(spec_cal.SEED_PAR_WEIGHT), rel=1e-6)


def test_text_mirror_rejects_a_missing_line_and_a_short_vector():
    without_weight = [line for line in spec_cal_text().splitlines()
                      if not line.startswith("par_weight")]
    with pytest.raises(ValueError, match="par_weight"):
        spec_cal.parse_spec_cal_text(without_weight)

    short = spec_cal_text().splitlines()
    short[1] = "spec_sens:1,2,3"
    with pytest.raises(ValueError, match="10 float"):
        spec_cal.parse_spec_cal_text(short)


def test_build_frame_is_always_nine_bytes():
    frame = spec_cal.build_frame(35)
    assert len(frame) == 9 and frame[0] == 0xA0 and frame[1] == 35
    assert frame[2:] == b"\x00" * 7
    assert spec_cal.build_frame(33, 4)[1:3] == bytes([33, 4])
    with pytest.raises(ValueError):
        spec_cal.build_frame(*range(9))


# ============================================================================
# Host recompute: the four footguns
# ============================================================================

def test_recompute_agrees_with_a_correct_firmware():
    reading, cal = consistent_pair(raw=[2000, 1500, 3000, 2500, 2200,
                                        1800, 1600, 1400, 300, 4000])
    report = spec_cal.verify_firmware_math(reading, cal)
    assert report["passed"], report["mismatches"]


def test_a_seconds_tick_bug_is_caught():
    # miniPar's *firmware* uses the seconds tick; ambit and the ams constants use
    # ms. A host or device on the wrong one is out by 1000x.
    reading, cal = consistent_pair(raw=[2000] * 10)
    wrong = spec_cal.decode_spec_raw(make_spec_raw(
        raw=[2000] * 10,
        chan=[v * 1000 for v in reading.chan],
        par=reading.par * 1000, par_tier2=reading.par_tier2 * 1000))
    report = spec_cal.verify_firmware_math(wrong, cal)
    assert not report["passed"]
    assert report["max_rel_error"] > 0.9


def test_a_gain_ordinal_used_as_a_multiplier_is_caught_at_a_non_pinned_gain():
    # At ordinal 2 the bug is invisible (2 is the enum's fixed point). At ordinal
    # 4 the true multiplier is 8, so treating the byte as a multiplier is 2x off.
    reading, cal = consistent_pair(raw=[2000] * 10, gain_low=4, gain_high=4)
    as_if_multiplier = spec_cal.decode_spec_raw(make_spec_raw(
        raw=[2000] * 10, gain_low=4, gain_high=4,
        chan=[v * 2.0 for v in reading.chan],
        par=reading.par * 2.0, par_tier2=reading.par_tier2 * 2.0))
    assert not spec_cal.verify_firmware_math(as_if_multiplier, cal)["passed"]

    at_pinned, cal2 = consistent_pair(raw=[2000] * 10, gain_low=2, gain_high=2)
    assert spec_cal.verify_firmware_math(at_pinned, cal2)["passed"]


def test_a_missed_nir_clear_swap_is_caught():
    # NIR and Clear carry the most dissimilar coefficients in every vector, so a
    # missed swap is silent in the data and loud only in a check like this.
    swapped_weight = list(spec_cal.SEED_PAR_WEIGHT)
    swapped_weight[8], swapped_weight[9] = swapped_weight[9], swapped_weight[8]
    swapped = spec_cal.decode_spec_cal(make_spec_cal(par_weight=swapped_weight))

    reading, cal = consistent_pair(raw=[2000, 1500, 3000, 2500, 2200,
                                        1800, 1600, 1400, 300, 4000])
    assert spec_cal.verify_firmware_math(reading, cal)["passed"]
    assert not spec_cal.verify_firmware_math(reading, swapped)["passed"]


def test_per_bank_divisor_mismatch_is_caught():
    # A firmware that divided NIR/Clear by gain_low instead of gain_high.
    reading, cal = consistent_pair(raw=[2000] * 10, gain_low=2, gain_high=4)
    bad_chan = list(reading.chan)
    for slot in (8, 9):
        bad_chan[slot] *= 4.0
    wrong = spec_cal.decode_spec_raw(make_spec_raw(
        raw=[2000] * 10, gain_low=2, gain_high=4,
        chan=bad_chan, par=reading.par, par_tier2=reading.par_tier2))
    assert not spec_cal.verify_firmware_math(wrong, cal)["passed"]


def test_a_clipped_point_is_excluded_from_the_fit_at_any_drive():
    # A clipped reading is off-model: the clip is the chain's only nonlinearity,
    # and it also breaks the constancy of SUM w_i * offset_i that lets miniPar's
    # par_weight be reused at all (plan 7c).
    dark = spec_cal.decode_spec_raw(make_spec_raw(raw=[0] * 10, clip_mask=0b1111111111))
    usable, reasons = spec_cal.usable_for_fit(dark)
    assert usable is False
    assert any("clipped" in r for r in reasons)

    lit = spec_cal.decode_spec_raw(make_spec_raw(raw=[2000] * 10, clip_mask=0))
    assert spec_cal.usable_for_fit(lit) == (True, [])

    saturated = spec_cal.decode_spec_raw(make_spec_raw(
        raw=[50000] * 10, flags=spec_cal.FLAG_SATURATED, sat_mask=0b1111111111))
    assert spec_cal.usable_for_fit(saturated)[0] is False

    faulted = spec_cal.decode_spec_raw(make_spec_raw(flags=spec_cal.FLAG_FAULT))
    assert spec_cal.usable_for_fit(faulted)[0] is False

    # The calibration bits are state, not faults: an unswept seeded device is
    # exactly what we are here to calibrate, so a clear bit8/bit9 - and an
    # all-zero flags word - must not exclude its own sweep points.
    unswept = spec_cal.decode_spec_raw(make_spec_raw(raw=[2000] * 10, flags=0))
    assert spec_cal.usable_for_fit(unswept) == (True, [])
    asat = spec_cal.decode_spec_raw(make_spec_raw(raw=[2000] * 10,
                                                 flags=spec_cal.FLAG_ASAT))
    assert spec_cal.usable_for_fit(asat)[0] is False


def test_fitting_a_clipped_dark_point_would_bias_the_intercept():
    # Quantifies the reason for the rule above.
    x = [120.0, 380.0, 640.0, 980.0, 1450.0]
    y = [1.0 * v + 7.983 for v in x]
    drive = [0.8, 2.4, 3.0, 4.0, 6.6]

    clean = spec_cal.assess_affine_fit(x, y, drive)
    assert clean["par_intercept"] == pytest.approx(7.983, rel=1e-9)

    # ... and with a clipped dark point (par_tier2 == 0, reference == 0) folded in,
    # the intercept is dragged toward zero: 7.983 -> 4.78, i.e. 40% low, while R^2
    # stays high enough that no gate would catch it.
    polluted = spec_cal.assess_affine_fit([0.0] + x, [0.0] + y, [0.0] + drive)
    bias = (clean["par_intercept"] - polluted["par_intercept"]) / clean["par_intercept"]
    assert bias > 0.3
    assert polluted["r2"] > 0.99            # nothing in the QC would flag it


def test_clip_mask_is_reproduced_in_near_darkness():
    # The ams offsets are 0.28-2.02 RAW COUNTS at the pinned 2x / 139 ms, so the
    # clip only bites in a light-tight fixture - but there it bites on every
    # channel, which is exactly the dark point of the sweep.
    reading, cal = consistent_pair(raw=[0] * 10)
    assert reading.clip_mask == 0b1111111111
    assert spec_cal.verify_firmware_math(reading, cal)["passed"]


def test_the_offset_term_tier3_absorbs_is_constant_and_matches_the_docs():
    # Plan 7c: par_weight was fitted against basic counts with NO offset
    # subtracted, while the firmware computes s = max(0, x - spec_offset). The
    # difference is a CONSTANT that par_intercept absorbs exactly - which is the
    # entire argument for reusing miniPar's vector. Check the constant.
    #
    # Pinned to the value the module's own docstrings quote (1.09 umol on the
    # constrained seed, 2.40 on the superseded OLS one). The pin is what keeps
    # prose and constants from drifting apart across a seed generation - it broke
    # on exactly this swap, which is the point.
    constant = math.fsum(w * o for w, o in zip(spec_cal.SEED_PAR_WEIGHT,
                                               spec_cal.SEED_SPEC_OFFSET))
    assert constant == pytest.approx(1.09, abs=0.01)

    superseded = math.fsum(
        w * o for w, o in zip(spec_cal.SUPERSEDED_PAR_WEIGHTS["minipar-2026-08-17-ols"],
                              spec_cal.SEED_SPEC_OFFSET))
    assert superseded == pytest.approx(2.40, abs=0.01)

    # And it really is constant: the same shift whatever the light level, as long
    # as nothing clips.
    cal = spec_cal.decode_spec_cal(make_spec_cal())
    zero_offset = spec_cal.decode_spec_cal(make_spec_cal(spec_offset=[0.0] * 10))
    for level in (2000, 8000, 30000):
        reading = spec_cal.decode_spec_raw(make_spec_raw(raw=[level] * 10))
        with_offset = spec_cal.recompute(reading, cal).par_tier2
        without = spec_cal.recompute(reading, zero_offset).par_tier2
        assert without - with_offset == pytest.approx(constant, abs=1e-4)


# ============================================================================
# The provisional-PAR determination
# ============================================================================

def test_a_seeded_never_swept_device_is_provisional():
    cal = spec_cal.decode_spec_cal(make_spec_cal(par_slope=1.0, par_intercept=0.0))
    reading = spec_cal.decode_spec_raw(make_spec_raw(flags=0))
    verdict = spec_cal.read_par_provisional(reading, cal)
    assert verdict["provisional"] is True
    assert verdict["flag_par_weight_is_fleet_fit"] is False
    assert verdict["flag_tier3_stored"] is False
    assert verdict["tier3_unset"] is True
    assert verdict["flag_vector_agreement"] is True
    assert set(verdict["on_seeds"]) == {"spec_sens", "par_weight"}


def test_a_swept_device_on_seeds_is_still_provisional_and_says_why():
    # bit9 set, bit8 clear: tier 3 done, tier 2 still borrowed. That is the
    # correct outcome for every device the Calibratron will produce until an ambit
    # fleet vector lands, so bit8 staying clear after a good sweep is not a bug.
    cal = spec_cal.decode_spec_cal(make_spec_cal(par_slope=1.23, par_intercept=7.9))
    reading = spec_cal.decode_spec_raw(make_spec_raw(flags=spec_cal.FLAG_TIER3_STORED))
    verdict = spec_cal.read_par_provisional(reading, cal)
    assert verdict["provisional"] is True
    assert verdict["tier3_unset"] is False
    assert verdict["flag_vector_agreement"] is True
    assert any("bit8" in r for r in verdict["reasons"])
    assert not any("bit9" in r for r in verdict["reasons"])


def test_a_fully_measured_device_is_not_provisional():
    cal = spec_cal.decode_spec_cal(make_spec_cal(
        spec_sens=[40.0] * 10, par_weight=[100.0] * 10,
        par_slope=1.1, par_intercept=3.0))
    reading = spec_cal.decode_spec_raw(make_spec_raw(
        flags=spec_cal.FLAG_PAR_WEIGHT_IS_FLEET_FIT | spec_cal.FLAG_TIER3_STORED))
    verdict = spec_cal.read_par_provisional(reading, cal)
    assert verdict["provisional"] is False
    assert verdict["flag_vector_agreement"] is True


def test_flags_disagreeing_with_the_vectors_is_surfaced():
    # bit9 claims a stored tier 3 while the read-back is identity, and bit8 claims
    # an ambit fit while par_weight is bit-for-bit the miniPar seed. Either means
    # the bits and NVS tell different stories, which must not resolve silently.
    cal = spec_cal.decode_spec_cal(make_spec_cal(par_slope=1.0, par_intercept=0.0))
    reading = spec_cal.decode_spec_raw(make_spec_raw(
        flags=spec_cal.FLAG_PAR_WEIGHT_IS_FLEET_FIT | spec_cal.FLAG_TIER3_STORED))
    verdict = spec_cal.read_par_provisional(reading, cal)
    assert verdict["flag_vector_agreement"] is False
    assert verdict["provisional"] is True
    assert any("bit9" in r for r in verdict["reasons"])
    assert any("bit8" in r for r in verdict["reasons"])


def test_provisional_falls_back_to_the_vectors_with_no_reading():
    cal = spec_cal.decode_spec_cal(make_spec_cal(par_slope=1.0, par_intercept=0.0))
    verdict = spec_cal.read_par_provisional(None, cal)
    assert verdict["provisional"] is True
    assert verdict["flag_tier3_stored"] is None
    assert any("identity" in r for r in verdict["reasons"])


# ============================================================================
# The tier-3 affine fit
# ============================================================================

def sweep(slope, intercept, tier2=(0.0, 120.0, 380.0, 640.0, 980.0, 1450.0)):
    y = [slope * x + intercept for x in tier2]
    drive = [0.0, 0.8, 2.4, 3.0, 4.0, 6.6]
    return list(tier2), y, drive


def test_affine_fit_recovers_slope_and_intercept():
    x, y, drive = sweep(1.25, 7.983)
    result = spec_cal.assess_affine_fit(x, y, drive)
    assert result["passed"], result["reasons"]
    assert result["par_slope"] == pytest.approx(1.25, rel=1e-9)
    assert result["par_intercept"] == pytest.approx(7.983, rel=1e-9)
    assert result["r2"] == pytest.approx(1.0)
    assert result["dark_residual"] == pytest.approx(0.0, abs=1e-9)


def test_the_plans_own_tier3_result_passes():
    # Plan 7c: running ambit's chain over miniPar's data and refitting tier 3
    # returns a = 1.0000, b = 7.983 - the dark-offset term (2.40) plus miniPar's
    # discarded tier-2 intercept (5.61). An origin-forced gate would have to
    # either reject that or fold b into a.
    x, y, drive = sweep(1.0000, 7.983)
    assert spec_cal.assess_affine_fit(x, y, drive)["passed"]


def test_origin_forcing_would_misplace_a_real_intercept():
    # The rationale for not reusing assess_origin_fit. With a true b = 7.983, an
    # origin-forced slope absorbs the offset into the gain: the error at full
    # scale stays small, but at the dimmest point it is large, because a constant
    # error has been turned into a proportional one.
    x, y, drive = sweep(1.0, 7.983)
    affine = spec_cal.assess_affine_fit(x, y, drive)

    forced = sum(a * b for a, b in zip(x, y)) / sum(a * a for a in x)
    dim = x[1]                                     # 120 tier-2 counts
    truth = 1.0 * dim + 7.983
    assert affine["par_slope"] * dim + affine["par_intercept"] == pytest.approx(truth)
    assert abs(forced * dim - truth) / truth > 0.05     # >5% at the dim end
    assert abs(forced * x[-1] - (1.0 * x[-1] + 7.983)) / (x[-1] + 7.983) < 0.01


def test_negative_par_tier2_at_the_dark_point_is_accepted():
    # The seeded par_weight is negative on NIR, so a legitimate dark reading can
    # come back slightly negative. One negative coefficient is enough - the
    # constrained seed removed F3/F5/F8 but not this case. Rejecting it would
    # throw away the one point that constrains the intercept.
    x = [-1.4, 120.0, 380.0, 640.0, 980.0, 1450.0]
    y = [1.0 * v + 7.983 for v in x]
    drive = [0.0, 0.8, 2.4, 3.0, 4.0, 6.6]
    result = spec_cal.assess_affine_fit(x, y, drive)
    assert result["passed"], result["reasons"]


def test_negative_reference_par_is_rejected():
    x, y, drive = sweep(1.0, 0.0)
    y[0] = -5.0
    assert not spec_cal.assess_affine_fit(x, y, drive)["passed"]


def test_nonlinear_sweep_is_rejected_on_r2():
    x, y, drive = sweep(1.0, 0.0)
    y[3] *= 1.4                                    # a stuck reference reading
    result = spec_cal.assess_affine_fit(x, y, drive)
    assert not result["passed"]
    assert any("R-squared" in r or "residual" in r or "RMSE" in r
               for r in result["reasons"])


def test_non_monotonic_reference_is_rejected():
    x, y, drive = sweep(1.0, 0.0)
    y[2], y[4] = y[4], y[2]
    result = spec_cal.assess_affine_fit(x, y, drive)
    assert not result["passed"]
    assert any("monotonic" in r for r in result["reasons"])


def test_slope_and_intercept_bounds_mirror_the_firmware_predicates():
    # valid_par_slope: finite, (0, 100]. valid_par_intercept: finite, |b| <= 500.
    assert spec_cal.valid_par_slope(1.0) and spec_cal.valid_par_slope(100.0)
    assert not spec_cal.valid_par_slope(0.0)
    assert not spec_cal.valid_par_slope(100.1)
    assert not spec_cal.valid_par_slope(float("nan"))
    assert spec_cal.valid_par_intercept(-500.0) and spec_cal.valid_par_intercept(0.0)
    assert not spec_cal.valid_par_intercept(500.1)
    assert not spec_cal.valid_par_intercept(float("inf"))

    # A negative slope is a fit the device would refuse; catch it host-side.
    x, y, drive = sweep(1.0, 0.0)
    result = spec_cal.assess_affine_fit(x, list(reversed(y)), drive)
    assert not result["passed"]


def test_too_few_points_is_rejected():
    result = spec_cal.assess_affine_fit([0.0, 100.0, 200.0], [8.0, 108.0, 208.0],
                                        [0.0, 1.0, 2.0])
    assert not result["passed"]
    assert any("sweep points" in r for r in result["reasons"])


def test_clustered_sweep_is_rejected():
    x = [500.0] * 6
    y = [508.0] * 6
    assert not spec_cal.assess_affine_fit(x, y, [0.0, 0.8, 2.4, 3.0, 4.0, 6.6])["passed"]


def test_an_unconstrained_intercept_gets_a_note_not_a_rejection():
    # b large relative to the dimmest lit point: legal, but the operator should
    # extend the sweep downwards.
    x = [0.0, 40.0, 380.0, 640.0, 980.0, 1450.0]
    y = [80.0 + v for v in x]
    result = spec_cal.assess_affine_fit(x, y, [0.0, 0.8, 2.4, 3.0, 4.0, 6.6])
    assert result["passed"], result["reasons"]
    assert any("dimmest" in n for n in result["notes"])


def test_slope_outside_the_expected_band_gets_a_note():
    x, y, drive = sweep(12.0, 0.0)
    result = spec_cal.assess_affine_fit(x, y, drive)
    assert result["passed"], result["reasons"]
    assert any("outside the expected" in n for n in result["notes"])


# ============================================================================
# Spectral drift
# ============================================================================

def test_spectral_drift_is_zero_for_a_pure_intensity_ray():
    shape = [2000, 1500, 3000, 2500, 2200, 1800, 1600, 1400, 300, 4000]
    spectra = [[v * k for v in shape] for k in (0.25, 0.5, 1.0, 2.0)]
    drift = spec_cal.spectral_drift(spectra)
    assert drift["max_shape_deviation"] == pytest.approx(0.0, abs=1e-12)


def test_spectral_drift_sees_a_colour_temperature_shift():
    # A halogen lamp driven from 0.4 A to 6.6 A moves red-heavy to less so. Both
    # instruments see it and use the same tier-2 weights, so it largely cancels -
    # but it bounds how far the fitted slope travels from this lamp.
    cold = [3000, 2500, 2600, 2200, 1800, 1400, 1100, 900, 200, 4000]
    warm = [900, 1100, 1400, 1800, 2200, 2600, 3000, 3200, 900, 4000]
    drift = spec_cal.spectral_drift([cold, warm])
    assert drift["max_shape_deviation"] > 0.02
    assert len(drift["per_channel_span"]) == spec_cal.N_CHANNELS


def test_spectral_drift_needs_two_usable_spectra():
    assert spec_cal.spectral_drift([]) is None
    assert spec_cal.spectral_drift([[0] * 10, [0] * 10]) is None
    assert spec_cal.spectral_drift([[1] * 10]) is None


# ============================================================================
# Channel-order bookkeeping
# ============================================================================

def test_ambit_and_minipar_orders_differ_only_in_the_last_two_slots():
    assert spec_cal.CHANNELS[:8] == spec_cal.MINIPAR_CHANNELS[:8]
    assert spec_cal.CHANNELS[8:] == ("nir_910", "clear")
    assert spec_cal.MINIPAR_CHANNELS[8:] == ("clear", "nir_910")


def test_the_shipped_seeds_satisfy_the_firmware_predicate_ranges():
    # Plan section 7e: the largest values are 72.7 (spec_sens, limit 1000) and
    # 85.9 (par_weight, limit 1e4), so no predicate change was needed to seed.
    # The constrained seed only widened that margin - the OLS one peaked at 333.5.
    assert all(math.isfinite(v) and 0.0 <= v < 1.0 for v in spec_cal.SEED_SPEC_OFFSET)
    assert all(math.isfinite(v) and 0.0 < v <= 1000.0 for v in spec_cal.SEED_SPEC_SENS)
    assert all(math.isfinite(v) and abs(v) <= 1e4 for v in spec_cal.SEED_PAR_WEIGHT)
    assert max(spec_cal.SEED_SPEC_SENS) == pytest.approx(72.697997)
    assert max(spec_cal.SEED_PAR_WEIGHT) == pytest.approx(85.8748691)
    for vector in spec_cal.SUPERSEDED_PAR_WEIGHTS.values():
        assert all(math.isfinite(v) and abs(v) <= 1e4 for v in vector)


def test_only_nir_carries_a_negative_par_weight():
    """The physical sign rule, pinned so a future refit cannot quietly lose it.

    The eight band channels measure light and must lift PAR; Clear and NIR are
    broadband and appear in the model to subtract stray light and IR leakage, so
    only they may go negative. The superseded OLS seed broke this on F3, F5 and
    F8 - not spectral response but collinearity, condition number ~451 on a
    daylight-dominated set. Constraining F1-F8 >= 0 is what fixed it, so the
    constraint belongs in the test suite and not only in the fitting notebook.
    """
    weights = dict(zip(spec_cal.CHANNELS, spec_cal.SEED_PAR_WEIGHT))
    bands = [name for name in spec_cal.CHANNELS if name.startswith("f")]
    assert len(bands) == 8
    assert all(weights[name] >= 0.0 for name in bands), \
        {name: weights[name] for name in bands if weights[name] < 0}
    assert weights["nir_910"] < 0.0        # IR leakage subtraction

    # And the vector the constraint replaced did violate it, so this test would
    # have caught that seed rather than passing vacuously.
    old = dict(zip(spec_cal.CHANNELS,
                   spec_cal.SUPERSEDED_PAR_WEIGHTS["minipar-2026-08-17-ols"]))
    assert [name for name in bands if old[name] < 0] == ["f3_480", "f5_555", "f8_680"]


def test_a_device_on_the_superseded_seed_is_named_not_called_unknown():
    """A False in ``seed_match`` means "someone wrote a fitted vector here".

    So after a seed bump, a device still on the previous firmware must not land
    in that bucket - it would read as calibrated when it is merely stale. The
    generation lookup is what keeps the two apart.
    """
    old = list(spec_cal.SUPERSEDED_PAR_WEIGHTS["minipar-2026-08-17-ols"])
    stale = spec_cal.decode_spec_cal(make_spec_cal(par_weight=old))

    assert stale.seed_match()["par_weight"] is False
    assert stale.par_weight_generation() == "minipar-2026-08-17-ols"

    verdict = spec_cal.read_par_provisional(None, stale)
    assert verdict["par_weight_generation"] == "minipar-2026-08-17-ols"
    assert any("superseded" in r for r in verdict["reasons"])
    assert verdict["provisional"] is True


def test_the_current_seed_and_a_fitted_vector_are_told_apart():
    current = spec_cal.decode_spec_cal(make_spec_cal())
    assert current.par_weight_generation() == spec_cal.SEED_GENERATION
    assert current.to_dict()["par_weight_generation"] == spec_cal.SEED_GENERATION
    # No "superseded" reason for a device on the current generation.
    assert not any("superseded" in r
                   for r in spec_cal.read_par_provisional(None, current)["reasons"])

    fitted = spec_cal.decode_spec_cal(make_spec_cal(par_weight=[7.5] * 10))
    assert fitted.par_weight_generation() is None
    assert fitted.seed_match()["par_weight"] is False
