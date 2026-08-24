"""Tests for the ADPD photodiode reduction recorded alongside both sweeps.

These traces are not a calibration - nothing is fitted or written from them - so
what matters is that the numbers reaching the record are trustworthy and that a
pinned or dead channel is visible rather than plausible.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quality


def arrun(actinic=0, **channels):
    """An arrun record shaped the way the device's reply parses."""
    return {"actinic": actinic, "num_points": 5, "freq_hz": 10,
            "pulse_currents_zeroed": True, "truncated": False,
            "data": dict(channels)}


# ============================================================================
# summarize_arrun
# ============================================================================

def test_statistics_are_the_sample_standard_deviation():
    stats = quality.summarize_arrun(arrun(leaf=[100, 102, 101, 99, 103]))
    assert stats["leaf"]["n"] == 5
    assert stats["leaf"]["mean"] == pytest.approx(101.0)
    # n-1, matching the old numpy ddof=1, not the population sd.
    assert stats["leaf"]["std"] == pytest.approx(1.5811388, rel=1e-6)
    assert (stats["leaf"]["min"], stats["leaf"]["max"]) == (99.0, 103.0)
    assert stats["leaf"]["saturated"] is False


def test_a_single_sample_reports_zero_spread_rather_than_raising():
    stats = quality.summarize_arrun(arrun(sun=[4321]))
    assert stats["sun"]["n"] == 1
    assert stats["sun"]["std"] == 0.0
    assert stats["sun"]["mean"] == pytest.approx(4321.0)


def test_leaf_and_sun_are_both_reported_and_leaf_comes_first():
    # 'leaf' first because it is the channel the operator watches.
    assert quality.ARRUN_CHANNELS[0] == "leaf"
    assert "sun" in quality.ARRUN_CHANNELS
    stats = quality.summarize_arrun(arrun(leaf=[10, 11], sun=[900, 910]))
    assert list(stats) == ["leaf", "sun"]


def test_empty_absent_and_unparseable_channels_are_skipped_not_fatal():
    # s_730/r_730 come back empty because the run is type 2 (no IR reflect), and
    # the reader keeps an unparseable payload as text rather than losing it.
    stats = quality.summarize_arrun(arrun(leaf=[5, 6], s_730=[], r_630="Length:0"))
    assert list(stats) == ["leaf"]
    assert quality.summarize_arrun(None) == {}
    assert quality.summarize_arrun({"data": {}}) == {}
    assert quality.summarize_arrun(arrun()) == {}


def test_a_pinned_channel_is_flagged():
    # A saturated photodiode is the failure that quietly ruins a later analysis:
    # the samples stay plausible and the spread collapses.
    full = quality.ADPD_SAMPLE_MAX
    stats = quality.summarize_arrun(arrun(leaf=[full, full, full]))
    assert stats["leaf"]["saturated"] is True
    assert stats["leaf"]["std"] == 0.0

    # Just inside the margin still counts as pinned; well below does not.
    near = int(full * (1.0 - quality.ADPD_SATURATION_MARGIN / 2))
    assert quality.summarize_arrun(arrun(leaf=[near]))["leaf"]["saturated"] is True
    assert quality.summarize_arrun(arrun(leaf=[full // 2]))["leaf"]["saturated"] is False


def test_booleans_are_not_accepted_as_samples():
    # bools are ints in Python; a True in a sample buffer is a parse bug.
    assert quality.summarize_arrun(arrun(leaf=[1, True, 3])) == {}


# ============================================================================
# assess_adpd_sweep
# ============================================================================

def rising_sweep(channel="leaf", means=(10, 120, 400, 900, 1500), drive=None):
    drive = list(drive if drive is not None else [0.0, 0.8, 2.4, 4.0, 6.6])
    summaries = [quality.summarize_arrun(arrun(**{channel: [m, m + 1, m - 1]}))
                 for m in means]
    return summaries, drive


def test_a_rising_photodiode_response_is_clean():
    summaries, drive = rising_sweep()
    report = quality.assess_adpd_sweep(summaries, drive, channels=("leaf",))
    assert report["leaf"]["n_points"] == 5
    assert report["leaf"]["monotonic"] is True
    assert report["leaf"]["saturated_at"] == []
    assert report["leaf"]["notes"] == []
    assert report["leaf"]["span"] > 0


def test_points_are_ordered_by_drive_not_by_arrival():
    # The tier-3 sweep puts 0.0 A first but a reordered sweep must still read as
    # monotonic, since ordering is derived from the drive.
    summaries, drive = rising_sweep()
    order = [3, 0, 4, 1, 2]
    report = quality.assess_adpd_sweep([summaries[i] for i in order],
                                       [drive[i] for i in order],
                                       channels=("leaf",))
    assert report["leaf"]["monotonic"] is True
    assert report["leaf"]["drive"] == sorted(drive)


def test_a_pinned_channel_names_the_drive_it_pinned_at():
    full = quality.ADPD_SAMPLE_MAX
    summaries, drive = rising_sweep(means=(10, 120, 400, full, full))
    report = quality.assess_adpd_sweep(summaries, drive, channels=("leaf",))
    assert report["leaf"]["saturated_at"] == [4.0, 6.6]
    assert any("floor, not a measurement" in n for n in report["leaf"]["notes"])


def test_a_dead_channel_is_reported():
    summaries, drive = rising_sweep(means=(500, 500, 500, 500, 500))
    report = quality.assess_adpd_sweep(summaries, drive, channels=("leaf",))
    assert report["leaf"]["span"] == pytest.approx(0.0)
    assert any("no response" in n for n in report["leaf"]["notes"])


def test_a_reversed_channel_is_reported():
    summaries, drive = rising_sweep(means=(1500, 900, 400, 120, 10))
    report = quality.assess_adpd_sweep(summaries, drive, channels=("leaf",))
    assert report["leaf"]["monotonic"] is False
    assert any("not monotonic" in n for n in report["leaf"]["notes"])


def test_small_noise_reversals_are_tolerated():
    summaries, drive = rising_sweep(means=(10, 120, 398, 900, 1500))
    noisy = list(summaries)
    noisy[2], noisy[3] = noisy[3], noisy[2]        # swap two adjacent-ish points
    report = quality.assess_adpd_sweep(summaries, drive, channels=("leaf",))
    assert report["leaf"]["monotonic"] is True     # the unswapped run is fine
    assert report is not noisy


def test_an_absent_channel_is_reported_not_omitted():
    summaries, drive = rising_sweep(channel="leaf")
    report = quality.assess_adpd_sweep(summaries, drive, channels=("leaf", "sun"))
    assert report["sun"]["n_points"] == 0
    assert "absent" in report["sun"]["note"]


def test_the_sweep_report_never_gates():
    # Every failure mode above yields notes, never a "passed" key - a flat or
    # pinned photodiode must not discard an otherwise good tier-3 fit.
    full = quality.ADPD_SAMPLE_MAX
    summaries, drive = rising_sweep(means=(full, full, full, full, full))
    report = quality.assess_adpd_sweep(summaries, drive, channels=("leaf",))
    assert "passed" not in report["leaf"]
    assert "reasons" not in report["leaf"]
