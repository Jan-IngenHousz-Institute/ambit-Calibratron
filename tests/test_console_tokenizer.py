"""The command templates, checked against a model of the device's tokenizer.

`ambit-1.ino` reads each command with `Serial_Input_Chars(choose, ":,", 200, ...)`
and `serial.cpp` gives that reader three properties worth modelling:

1. `:` and `,` terminate a token and are thrown away.
2. CR and LF are skipped WITHOUT being stored and without restarting the
   inter-character timer.
3. Anything else - a space very much included - is stored, and storing restarts a
   200 ms window during which more bytes join the same token.

So a trailing space in a command's padding merges with whatever the host sends
next: the verb arrives as " set_currents", `do_command()` drops it silently
because `isalnum(' ')` is false, and its arguments then dispatch as commands. With
no numeric `case` in the switch, each one answers BAD COMMAND. Six of seven
`set_currents` calls were lost that way, leaving the ADPD pulse LEDs driving
through a whole sweep while the traces looked normal.

The model below deliberately omits the timeout: no delimiter between two tokens
means they CAN merge, and a template must not depend on the host being slow.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import helpers

PROTO = helpers.AmbitProto


def device_tokens(stream):
    """Tokens ambit's reader would produce from `stream`. See module docstring."""
    tokens, current = [], []
    for char in stream:
        if char in ":,":
            tokens.append("".join(current))
            current = []
        elif char in "\r\n":
            continue
        else:
            current.append(char)
    if current:
        tokens.append("".join(current))
    return tokens


def dispatchable(token):
    """Would `do_command()` act on this token? Mirrors its two early returns."""
    printable = "".join(c for c in token if c.isprintable())
    stripped = printable.split("\x00")[0]
    return bool(stripped) and stripped[0].isalnum()


ARRUN2 = PROTO.ARRUN2.format(nh=0, nl=5, fh=0, fl=10, act=0)
ARRUN1 = PROTO.LED_RUN.format(led=250)
CURRENTS = PROTO.SET_CURRENTS.format(i620=0, i720=0, ir=0)


# ============================================================================
# The bug, stated as a test
# ============================================================================

@pytest.mark.parametrize("padded", [ARRUN2, ARRUN1], ids=["arrun2", "arrun1"])
def test_the_next_verb_survives_a_padded_command(padded):
    tokens = device_tokens(padded + CURRENTS)
    assert "set_currents" in tokens, tokens


@pytest.mark.parametrize("padded", [ARRUN2, ARRUN1], ids=["arrun2", "arrun1"])
def test_no_padding_token_is_dispatchable(padded):
    # Everything after the last field must be inert: not acted on, not merged.
    trailing = device_tokens(padded)[11:]        # verb + 10 fields consumed
    assert not [t for t in trailing if dispatchable(t)], trailing


def test_the_old_padding_would_have_failed_this(): 
    # The exact template that produced six BAD COMMANDs a run, so the guard is
    # shown to catch the thing it was written for rather than merely passing.
    old = "arrun2,1,0,2,0,0,5,0,10,0,1," + "\n, \n"
    tokens = device_tokens(old + CURRENTS)
    assert "set_currents" not in tokens
    assert " set_currents" in tokens
    assert not dispatchable(" set_currents")     # silently dropped by the device
    assert dispatchable("0")                     # ...and its arguments are not


# ============================================================================
# Field counts: a miscount leaves numbers to be read as commands
# ============================================================================

@pytest.mark.parametrize("rendered,expected", [(ARRUN2, 10), (ARRUN1, 10)],
                         ids=["arrun2", "arrun1"])
def test_exactly_the_fields_the_firmware_consumes_are_sent(rendered, expected):
    # Both verbs read `len`, `persist`, then len*8 (do_command.h). len is the
    # first field, so with len=1 that is 2 + 8 = 10 numeric fields. One too many
    # and the surplus dispatches as a command; one too few and a field reads 0.
    fields = device_tokens(rendered)[1:1 + expected]
    assert len(fields) == expected
    assert all(f.isdigit() for f in fields), fields
    assert int(fields[0]) == 1, "len is what multiplies the 8-field block"


def test_set_currents_sends_three_terminated_fields():
    tokens = device_tokens(CURRENTS)
    assert tokens[0] == "set_currents"
    assert tokens[1:4] == ["0", "0", "0"]
