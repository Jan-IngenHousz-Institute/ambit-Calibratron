"""Tests for the bench-instrument transport.

The failure these guard against is not a crash but a *plausible* number: the
MiniPARs are ESP32-class boards whose reset pin hangs off DTR/RTS, so an open
that asserts those lines reboots the instrument, and the line that comes back is
bootloader output, an ``error:`` from a half-swallowed command, or a stale reply
to the previous question. Every one of those either parses as the wrong PAR or
poisons a sweep point that then gets fitted.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import helpers


class FakeSerial:
    """A scripted port. Each write pops one list of lines to reply with."""

    def __init__(self, script, pending=()):
        self.script = list(script)
        self.buffer = [l.encode() + b"\n" for l in pending]
        self.writes = []
        self.drained = 0
        self.closed = False

    # -- transport -------------------------------------------------------
    def write(self, data):
        self.writes.append(data)
        reply = self.script.pop(0) if self.script else []
        self.buffer += [l.encode() + b"\n" for l in reply]
        return len(data)

    def readline(self):
        return self.buffer.pop(0) if self.buffer else b""   # b"" == read timeout

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.drained += 1
        self.buffer = []

    def reset_output_buffer(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


@pytest.fixture
def bench(monkeypatch):
    """Install a FakeSerial factory; return a setup(script, pending) helper."""
    holder = {}

    def setup(script, pending=()):
        ser = FakeSerial(script, pending)
        holder["ser"] = ser
        monkeypatch.setattr(helpers, "open_serial_no_reset",
                            lambda port, timeout=2.0: ser)
        monkeypatch.setattr(helpers, "QUERY_RETRY_S", 0)
        return ser

    return setup


BOOT_LOG = ["ESP-ROM:esp32c3-api1-20210207", "Saved PC:0x40053b60", "SPIWP:0xee",
            "load:0x3fcd5810,len:0x438", "entry 0x403cc710"]


# ============================================================================
# Boot chatter
# ============================================================================

@pytest.mark.parametrize("line", BOOT_LOG + ["mode:DIO, clock div:1", "ets Jul 29 2019",
                                             "I (245) ambit: ready", "csum 0x8f"])
def test_boot_log_is_recognised(line):
    assert helpers.is_boot_chatter(line)


@pytest.mark.parametrize("line", ["5.59", "-0.001", "Par_REF", "model=AS7341,atime=99",
                                  "slope=1.02,intercept=-0.4", "error:unknown_command"])
def test_replies_are_not_mistaken_for_boot_log(line):
    assert not helpers.is_boot_chatter(line)


def test_boot_chatter_is_skipped_without_costing_an_attempt(bench):
    ser = bench([BOOT_LOG + ["5.59"]])
    assert helpers.get_par_MP("COM64") == 5.59
    assert len(ser.writes) == 1


# ============================================================================
# Retries
# ============================================================================

def test_mangled_command_is_asked_again(bench):
    # `error:unknown_command` is what the MiniPAR says when the command line it
    # received was chewed up by a reset - the question is worth repeating.
    ser = bench([["error:unknown_command"], ["253.18"]])
    assert helpers.get_par_MP("COM3") == 253.18
    assert len(ser.writes) == 2


def test_a_reply_of_the_wrong_shape_is_asked_again(bench):
    # A stale line from the *previous* question is the dangerous case: it parses.
    ser = bench([["model=AS7341,available=1"], ["17.056"]])
    assert helpers.get_par_MP("COM64") == 17.056
    assert len(ser.writes) == 2


def test_a_silent_port_is_retried_then_reported(bench):
    ser = bench([[], [], []])
    with pytest.raises(helpers.ReferenceUnavailable) as exc:
        helpers.get_par_MP("COM64")
    assert len(ser.writes) == helpers.QUERY_ATTEMPTS
    assert "COM64" in str(exc.value)


def test_stale_bytes_are_dropped_before_every_question(bench):
    # Whatever is already buffered predates the question, so it cannot answer it.
    ser = bench([["12.5"]], pending=["99.9"])
    assert helpers.get_par_MP("COM64") == 12.5
    assert ser.drained >= 1


def test_the_port_is_closed_even_when_no_reply_arrives(bench):
    ser = bench([[], [], []])
    with pytest.raises(helpers.ReferenceUnavailable):
        helpers.get_par_MP("COM64")
    assert ser.closed


# ============================================================================
# The reference reads that used to return boot chatter
# ============================================================================

def test_spec_raw_survives_a_reset_on_the_first_attempt(bench):
    bench([["load:0x403cc710,len:0x90c"],
           ["AS7341,1,2,3,4,5,6,7,8,900,1000"]])
    out = helpers.get_spec_raw_MP("COM64")
    assert out["model"] == "AS7341"
    assert out["counts"][-2:] == [900, 1000]


def test_spec_status_survives_a_reset_on_the_first_attempt(bench):
    bench([["Saved PC:0x40053b60", "error:unknown_command"],
           ["model=AS7341,available=1,atime=99,astep=999,gain=256"]])
    assert helpers.get_spec_status_MP("COM64") == {
        "model": "AS7341", "available": 1, "atime": 99, "astep": 999, "gain": 256}


def test_cal_par_survives_a_reset_on_the_first_attempt(bench):
    bench([["SPIWP:0xee"], ["slope=1.0234,intercept=-0.4"]])
    assert helpers.get_cal_par_MP("COM64") == {"slope": 1.0234, "intercept": -0.4}


def test_spec_coeff_rejects_a_vector_cut_short(bench):
    # A line truncated mid-print still parses as floats, which is why the
    # channel count is what gets checked.
    full = ",".join(f"0.{n:02d}" for n in range(1, 19))
    bench([["0.01,0.02,0.03"], [full]])
    assert len(helpers.get_spec_coeff_MP("COM64")) == 18


def test_unreadable_optional_reads_degrade_to_none_and_report_what_was_heard(bench, caplog):
    bench([BOOT_LOG, BOOT_LOG, BOOT_LOG])
    with caplog.at_level("WARNING"):
        assert helpers.get_spec_status_MP("COM64") is None
    assert "unavailable" in caplog.text


# ============================================================================
# The DC source is deliberately NOT on the no-reset path
# ============================================================================

def test_the_dc_source_keeps_the_default_dtr_handling(monkeypatch):
    # The Kiprim is not an ESP32 and some bridges hold output until DTR is
    # asserted, so `_command` must stay on pyserial's default open.
    opened = {}

    def fake_serial(port, baudrate=None, **kw):
        opened["port"] = port
        return FakeSerial([])

    monkeypatch.setattr(helpers.serial, "Serial", fake_serial)
    monkeypatch.setattr(helpers, "open_serial_no_reset",
                        lambda *a, **k: pytest.fail("DC source must not use the "
                                                    "no-reset open"))
    helpers.set_current("COM22", 1.5)
    assert opened["port"] == "COM22"
