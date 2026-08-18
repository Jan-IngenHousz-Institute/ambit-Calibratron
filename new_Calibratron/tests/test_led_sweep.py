"""Tests for the actinic-LED sweep, and specifically for its silent failure.

The failure guarded against here is not a crash and not an implausible number -
it is a *constant* one. ``arrun1`` (the latch) parses every numeric field with
``Serial_Input_Long(",", 10)``, a 10 ms per-field timeout, and a field that does
not arrive in time reads back ``atol("") == 0``: ``len`` 0 skips the run, and
``persist`` 0 ends it with ``AS_LED_OFF()``. Either way the LED goes dark and
the device says nothing about it, so the reference reads its dark floor at every
remaining setting and the fit gate reports five failures that all describe a
constant regressor and none of which name the cause.

The interleaved sweep walked into that on every point, because the ADPD trace it
ran between latches is ``arrun2`` with ``persist=0``. Hence two passes, and hence
a latch verified against the reference instead of trusted.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import helpers
import run_calibratron as rc


class FakeLink:
    """An Ambit link that records what was asked of it, in order.

    ``lit_settings`` is the set of settings at which the latch actually holds;
    anything else leaves the reference in the dark, which is the whole failure.
    """

    def __init__(self, lit_settings=None, run_confirmed=True):
        self.lit = None if lit_settings is None else set(lit_settings)
        self.run_confirmed = run_confirmed
        self.calls = []                 # ("actinic", s) / ("arrun", s), in order
        self.actinic = 0

    def set_actinic(self, setting, timeout=4.0):
        self.calls.append(("actinic", setting))
        holds = self.lit is None or setting in self.lit
        self.actinic = setting if holds else 0
        return self.run_confirmed

    def arrun(self, actinic=0, num_points=5, freq=10, timeout=15.0):
        self.calls.append(("arrun", actinic))
        # arrun2 passes persist=0, so the trace ends with the LED off.
        self.actinic = 0
        return {"actinic": actinic, "data": {"leaf": [800 + actinic] * num_points}}


DARK_PAR = 5.91
DARK_COUNTS = [1, 3, 4, 6, 7, 10, 11, 8, 22, 3]


@pytest.fixture
def reference(monkeypatch):
    """Wire the MiniPAR readers to a FakeLink's actual LED state.

    The reference is the only witness to whether the latch held - the device
    cannot report ``persist`` back - so the test reads it the same way.
    """
    def install(link, gain=10.0):
        monkeypatch.setattr(helpers, "get_par_MP",
                            lambda port: DARK_PAR + gain * link.actinic)
        monkeypatch.setattr(
            helpers, "get_spec_raw_MP",
            lambda port: {"model": "MP", "counts":
                          ([1000, 0, 0, 0, 0, 440, 1231, 52, 799, 75]
                           if link.actinic else list(DARK_COUNTS))})
        monkeypatch.setattr(rc.time, "sleep", lambda s: None)
        return link
    return install


SETTINGS = [10, 20, 60, 90, 150, 250, 0]


def sweep(link, settings=SETTINGS, **kwargs):
    measured, ref_spec, latch = [], [], []
    dark = rc._led_reference_sweep(link, "COM_EMIT", settings,
                                   measured, ref_spec, latch, **kwargs)
    return measured, ref_spec, latch, dark


# ============================================================================
# The dark floor
# ============================================================================

def test_the_dark_floor_is_measured_not_assumed(reference):
    """The threshold is derived from the reference's own reading at zero drive.

    The MiniPAR reports ``par_raw * slope + intercept``, so its floor is a
    property of that instrument, not a constant this script gets to hardcode.
    """
    link = reference(FakeLink())
    _measured, _spec, _latch, dark = sweep(link)

    assert dark["par"] == pytest.approx(DARK_PAR)
    assert dark["counts"] == DARK_COUNTS
    assert dark["response_threshold"] == pytest.approx(
        max(rc.LED_MIN_RESPONSE, rc.LED_MIN_RESPONSE_FRACTION * DARK_PAR))
    assert link.calls[0] == ("actinic", 0)      # parked before the floor is read


# ============================================================================
# Latch verification
# ============================================================================

def test_a_holding_latch_is_read_once_per_setting(reference):
    link = reference(FakeLink())
    measured, _spec, latch, _dark = sweep(link)

    assert [e["attempts"] for e in latch] == [1] * len(SETTINGS)
    assert [e["lit"] for e in latch] == [True] * 6 + [False]   # 0 is legitimately dark
    assert measured[0] > measured[-1]


def test_a_dropped_latch_is_retried_and_then_reported(reference):
    """The 2026-08-18 failure: lit at the first setting, dark at every later one."""
    link = reference(FakeLink(lit_settings={10}))
    measured, ref_spec, latch, _dark = sweep(link, attempts=3)

    assert measured[0] == pytest.approx(DARK_PAR + 10 * 10.0)
    assert measured[1:] == pytest.approx([DARK_PAR] * 6)       # the constant regressor
    assert ref_spec[1:] == [DARK_COUNTS] * 6

    assert [e["attempts"] for e in latch] == [1, 3, 3, 3, 3, 3, 1]
    assert [e["lit"] for e in latch] == [True, False, False, False, False, False, False]
    assert [e["setting"] for e in latch if e["expected_light"] and not e["lit"]] \
        == [20, 60, 90, 150, 250]


def test_a_latch_that_takes_on_the_second_try_is_not_reported_as_failed(reference):
    """A retried-and-recovered point is a good point, not a caveat."""
    seen = set()

    class Flaky(FakeLink):
        def set_actinic(self, setting, timeout=4.0):
            # Every setting's first latch drops; the retry holds. Counted per
            # setting, not per call: the dark-floor park is a call too.
            self.lit = {setting} if setting in seen else set()
            seen.add(setting)
            return super().set_actinic(setting, timeout)

    link = reference(Flaky())
    _measured, _spec, latch, _dark = sweep(link, settings=[60, 150], attempts=3)

    assert [e["attempts"] for e in latch] == [2, 2]
    assert all(e["lit"] for e in latch)


def test_settings_the_firmware_forces_off_are_not_retried(reference):
    """``if (actinic > 3)`` in ``run_arr_type1``: at or below 3 dark is correct.

    Retrying there would spend three latches per point chasing a reading the
    firmware is contractually obliged to give.
    """
    link = reference(FakeLink(lit_settings=set()))
    _measured, _spec, latch, _dark = sweep(link, settings=[0, 3, 4], attempts=3)

    assert [e["expected_light"] for e in latch] == [False, False, True]
    assert [e["attempts"] for e in latch] == [1, 1, 3]


def test_an_unconfirmed_arrun1_is_carried_into_the_record(reference):
    """``set_actinic`` returning False means no sampled point came back."""
    link = reference(FakeLink(run_confirmed=False))
    _measured, _spec, latch, _dark = sweep(link, settings=[60])

    assert latch[0]["run_confirmed"] is False


# ============================================================================
# Two passes, in that order
# ============================================================================

def test_every_reference_read_happens_before_the_first_arrun(reference,
                                                             monkeypatch):
    """The ordering guarantee, asserted rather than left to the call site.

    ``arrun2`` ends with ``AS_LED_OFF()``, so a single ``arrun`` between two
    reference reads is enough to reintroduce the original failure.
    """
    link = reference(FakeLink())
    monkeypatch.setattr(rc.helpers, "AmbitLink", lambda port: _as_cm(link))
    monkeypatch.setattr(rc.helpers, "set_ambit_led_gain",
                        lambda port, coeff: None)
    monkeypatch.setattr(rc.helpers, "ambit_reboot",
                        lambda port: _Info(0.1))

    rc.calibrate_led("COM_AMBIT", "COM_EMIT", upload=False, current_coeff=0.1)

    kinds = [kind for kind, _s in link.calls]
    assert "arrun" in kinds, "the ADPD pass should still run"
    first = kinds.index("arrun")

    # Every setting is latched, and every latch precedes the first ADPD trace.
    assert [s for k, s in link.calls[:first] if k == "actinic"] == [0] + SETTINGS
    # After that, only traces - plus the final park at 0.
    assert [s for k, s in link.calls[first:] if k == "arrun"] == SETTINGS
    assert [s for k, s in link.calls[first:] if k == "actinic"] == [0]


def test_the_record_names_the_settings_where_the_latch_failed(reference,
                                                              monkeypatch):
    """So the JSON says *why*, not only that five thresholds were missed."""
    link = reference(FakeLink(lit_settings={10}))
    monkeypatch.setattr(rc.helpers, "AmbitLink", lambda port: _as_cm(link))

    record = rc.calibrate_led("COM_AMBIT", "COM_EMIT", upload=False,
                              current_coeff=0.1)

    assert record["latch_failed_at"] == [20, 60, 90, 150, 250]
    assert record["dark_reference"]["par"] == pytest.approx(DARK_PAR)
    assert record["fit"]["passed"] is False
    assert record["uploaded"] is False


def test_a_reference_that_dies_mid_sweep_keeps_the_partial_record(reference,
                                                                 monkeypatch):
    link = reference(FakeLink())
    monkeypatch.setattr(rc.helpers, "AmbitLink", lambda port: _as_cm(link))

    calls = {"n": 0}

    def dying(port):
        calls["n"] += 1
        if calls["n"] > 3:                  # floor + two settings, then gone
            raise helpers.ReferenceUnavailable("MiniPAR stopped answering")
        return DARK_PAR + 10.0 * link.actinic

    monkeypatch.setattr(helpers, "get_par_MP", dying)
    record = rc.calibrate_led("COM_AMBIT", "COM_EMIT", upload=False,
                              current_coeff=0.1)

    assert record["status"] == "reference_unavailable"
    assert record["led_settings"] == SETTINGS[:2]
    assert len(record["ref_par"]) == 2
    assert record["dark_reference"] is None     # raised before the sweep returned


# ---- small stand-ins -------------------------------------------------------

class _Info:
    def __init__(self, coeff):
        self.act_led_coeff = coeff


class _as_cm:
    """Hand the same FakeLink to a ``with helpers.AmbitLink(...)`` block."""

    def __init__(self, link):
        self.link = link

    def __enter__(self):
        return self.link

    def __exit__(self, *exc):
        return False
