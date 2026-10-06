"""Tests for reading an Ambit console echo out of a noisy console.

`reset_input_buffer` drops what has already arrived, not what is still coming.
The firmware guarantees there WILL be late output: `Serial_Input_Long` gives each
numeric field a 10 ms timeout, so an arrun line split across USB packets leaves
its tail unconsumed and the tail dispatches as a command - a numeric token goes
through `atoi` into a switch with no such case and the device prints BAD COMMAND.
Reading exactly one line then blames the next command for the previous one's
residue, which is how `set_currents` looked unsupported while the firmware has
implemented it all along (ambit/src/do_command.h, case hash("set_currents")).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import helpers


class FakeConsole:
    """A serial stand-in whose reply to each write is scripted."""

    def __init__(self, script):
        self.script = list(script)
        self.buffer = []
        self.writes = []
        self.timeout = 2.0

    def write(self, data):
        self.writes.append(data.decode())
        reply = self.script.pop(0) if self.script else []
        self.buffer += [l.encode() + b"\n" for l in reply]
        return len(data)

    def readline(self):
        return self.buffer.pop(0) if self.buffer else b""

    def reset_input_buffer(self):
        self.buffer = []

    def flush(self):
        pass


def link_with(script):
    link = helpers.AmbitLink("COM_TEST")
    link._ser = FakeConsole(script)
    return link


# ============================================================================
# text(expect=...)
# ============================================================================

def test_the_expected_echo_is_found_behind_stale_console_output():
    link = link_with([["BAD COMMAND", "Done", "Currents set to 0, 0, 0"]])
    assert link.zero_pulse_currents() is True


def test_leftover_plot_lines_do_not_hide_the_echo():
    link = link_with([["T:123,456", "T:124,457", "Currents set to 0, 0, 0"]])
    assert link.zero_pulse_currents() is True


def test_a_genuinely_missing_echo_still_fails():
    link = link_with([["BAD COMMAND", "BAD COMMAND"]])
    assert link.zero_pulse_currents() is False


def test_the_failure_says_what_was_heard_and_what_it_costs(caplog):
    link = link_with([["BAD COMMAND"]])
    with caplog.at_level("WARNING"):
        link.zero_pulse_currents()
    assert "BAD COMMAND" in caplog.text
    assert "pulse LEDs" in caplog.text          # names the consequence, not just the reply


def test_expect_returns_the_last_line_seen_when_nothing_matches():
    link = link_with([["BAD COMMAND", "Done"]])
    assert link.text("set_currents,0,0,0,\n", timeout=0.1, expect="Currents set") == "Done"


def test_without_expect_the_first_line_is_still_returned_verbatim():
    # The n_lines contract is unchanged; only callers that pass `expect` drain.
    link = link_with([["Done", "ignored"]])
    assert link.text("reboot\n") == "Done"


def test_the_pulse_led_flag_follows_the_echo():
    link = link_with([["BAD COMMAND"], ["Data:leaf,Length:2\t1,2,", "Data sent"]])
    trace = link.arrun(actinic=0, num_points=2, freq=10, timeout=1.0)
    assert trace["pulse_currents_zeroed"] is False
