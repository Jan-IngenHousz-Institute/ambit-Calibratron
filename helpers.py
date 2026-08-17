"""
Helper functions for Ambit calibration and PAR measurements.

This module contains utilities for:
- Serial device communication and discovery
- PAR (Photosynthetically Active Radiation) measurements
- Data analysis and visualization
- Device calibration
"""

import os
import sys
import time
import json
import logging
import serial
import serial.tools.list_ports
import subprocess
import importlib.util
import glob
import hashlib
from datetime import datetime, timezone
from dataclasses import dataclass, field
from matplotlib import pyplot as plt
import numpy as np


class _UnicodeSafeHandler(logging.StreamHandler):
    """StreamHandler that falls back to ASCII+backslashreplace when the
    underlying stream's encoding (e.g. Windows cp1252) can't render a char.
    Without this, a stray byte like 0x80 in serial output crashes the logger.
    """
    def emit(self, record):
        try:
            msg = self.format(record) + self.terminator
            try:
                self.stream.write(msg)
            except UnicodeEncodeError:
                self.stream.write(msg.encode("ascii", "backslashreplace").decode("ascii"))
            self.flush()
        except Exception:
            self.handleError(record)


logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = _UnicodeSafeHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ============================================================================
# Time helpers
# ============================================================================

def iso_timestamp():
    """Return the current UTC time as an ISO 8601 / RFC 3339 string with
    millisecond precision and a trailing 'Z', e.g. ``'2025-09-16T10:45:21.861Z'``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ============================================================================
# Device Discovery & Communication
# ============================================================================

_PORTS_CACHE: "list | None" = None
_PORT_INFO_CACHE: "list | None" = None


def _invalidate_port_cache():
    """Clear the cached serial port list. Call after USB topology changes."""
    global _PORTS_CACHE, _PORT_INFO_CACHE
    _PORTS_CACHE = None
    _PORT_INFO_CACHE = None


def port_infos():
    """Return pyserial ``ListPortInfo`` objects for every port the OS reports.

    Memoised; call :func:`_invalidate_port_cache` after a USB topology change.

    :return: list of ListPortInfo, sorted by device name
    """
    global _PORT_INFO_CACHE
    if _PORT_INFO_CACHE is None:
        _PORT_INFO_CACHE = sorted(serial.tools.list_ports.comports(),
                                  key=lambda p: p.device)
    return list(_PORT_INFO_CACHE)


def serial_ports():
    """
    Lists available serial port names for the current platform.

    Uses the OS port enumeration (``serial.tools.list_ports``) rather than
    probing candidate names by opening them. On Windows the old approach opened
    COM1..COM256 one by one, which cost seconds per call and dominated the
    discovery time when calibrating several devices in a row.

    Memoised after the first call. Call _invalidate_port_cache() if devices
    have been hot-plugged since the last scan.

    :returns: A list of the serial ports available on the system
    """
    global _PORTS_CACHE
    if _PORTS_CACHE is None:
        _PORTS_CACHE = [p.device for p in port_infos()]
    return list(_PORTS_CACHE)


def findDevice(question="hello\r\n", answer="", flush=True, timeout=5, verbose=False):
    """
    Find Ambit device on available serial ports by handshake.

    Attempts to find a device by sending a 'question' string and looking for
    an 'answer' substring in the response.

    :param question: The message to send to the device (default: "hello")
    :param answer: The substring expected in the device response
    :param flush: Whether to flush the serial buffer before sending (default: True)
    :param timeout: The read timeout for the serial port in seconds (default: 5)
    :param verbose: When True, log the full handshake response (boot log and
        all); otherwise only the port that matched is reported (default: False)
    :return: The port where the device was found, or None if not found
    """
    for port in serial_ports():
        try:
            with serial.Serial(port, baudrate=115200, timeout=0.2) as ser:
                # Asserting DTR/RTS reboots devices like the ESP32-C3, so the
                # first thing we see is the boot log, not the handshake reply.
                ser.dtr = True
                ser.rts = True

                if flush:
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()

                # Give the DTR/RTS-triggered reboot a moment to start, then keep
                # re-sending the question and accumulating output until the
                # answer shows up or we run out of time. A single short read
                # would only catch the boot log and miss the (later) reply.
                time.sleep(0.3)
                deadline = time.time() + timeout
                msg = ""
                while time.time() < deadline:
                    ser.write(question.encode())
                    time.sleep(0.3)
                    msg_bytes = ser.read_all()
                    # Decode with unicode_escape for special characters; fall
                    # back to replacing undecodable bytes (e.g. reset framing
                    # noise) so a stray byte never aborts the scan.
                    try:
                        msg += msg_bytes.decode(encoding='unicode_escape')
                    except Exception:
                        msg += msg_bytes.decode(errors='replace')

                    if answer and answer in msg:
                        if verbose:
                            logger.info("Found device at: %s, answer: %s", port, msg)
                        else:
                            logger.info("Found device at: %s", port)
                        return port

                # No match on this port. Surface what it *did* say when verbose
                # is on (otherwise this stays at debug level and is hidden), so
                # the full response is visible even when the answer never shows.
                if verbose:
                    logger.info("No match on %s. Received: %s", port, msg.strip())
                else:
                    logger.debug("Received message: %s, port: %s", msg.strip(), port)
        except (OSError, serial.SerialException) as e:
            logger.debug("Cannot open %s: %s", port, e)
            _invalidate_port_cache()
            continue

    logger.warning("No matching device found")
    return None


def find_devices(specs, timeout=4.0, poll=0.25, ports=None, verbose=False):
    """Discover several serial devices in ONE pass over the available ports.

    :func:`findDevice` rescans every port for every role and burns the full
    ``timeout`` on each port that doesn't answer, so finding 4 instruments on 6
    ports costs up to ``4 * 6 * timeout`` seconds. This opens each port once,
    sends *all* still-unmatched questions on every poll, and stops probing a
    port as soon as one role matches - a device only ever fills one role. Cost
    drops to roughly one ``timeout`` per unidentified port, whatever the number
    of roles.

    All questions are newline-terminated commands, so interleaving them is safe:
    an instrument that doesn't recognise one just reports an unknown command.

    :param specs: ``{role: (question, answer)}``; ``answer`` is matched as a
        substring of everything the port has said so far
    :param timeout: seconds to keep polling a single port before giving up
    :param poll: seconds between question bursts
    :param ports: ports to probe (default: all present ports)
    :param verbose: log what each non-matching port actually said
    :return: ``{role: port}`` for every role in ``specs``; None where not found
    """
    pending = dict(specs)
    found = {}

    for port in (serial_ports() if ports is None else list(ports)):
        if not pending:
            break
        try:
            with serial.Serial(port, baudrate=BAUDRATE, timeout=0.2) as ser:
                # Asserting DTR/RTS reboots devices like the ESP32-C3, so the
                # first thing we see is the boot log, not the handshake reply.
                ser.dtr = True
                ser.rts = True
                ser.reset_input_buffer()
                ser.reset_output_buffer()
                time.sleep(0.3)

                msg = ""
                matched = None
                deadline = time.time() + timeout
                while time.time() < deadline and matched is None:
                    for question, _answer in pending.values():
                        ser.write(question.encode())
                    time.sleep(poll)
                    chunk = ser.read_all() or b""
                    try:
                        msg += chunk.decode(encoding="unicode_escape")
                    except Exception:
                        msg += chunk.decode(errors="replace")
                    for role, (_question, answer) in pending.items():
                        if answer and answer in msg:
                            matched = role
                            break

                if matched is not None:
                    logger.info("Found %s at: %s", matched, port)
                    found[matched] = port
                    del pending[matched]
                elif verbose:
                    logger.info("No match on %s. Received: %s", port, msg.strip())
                else:
                    logger.debug("No match on %s. Received: %s", port, msg.strip())
        except (OSError, serial.SerialException) as e:
            logger.debug("Cannot open %s: %s", port, e)
            _invalidate_port_cache()
            continue

    for role in pending:
        logger.warning("No device found for role %r", role)
        found[role] = None
    return found


# Remembers which port answered for which role, so a repeat run can re-check the
# known ports first instead of rescanning the whole bus. The bench peripherals
# (PAR reference, LED MiniPAR, DC source) stay plugged in while Ambits are
# swapped, so on the second device onwards this turns discovery into 3 quick
# confirmations plus a scan for the one port that moved.
PORT_ROLE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    ".port_roles.json")


def load_port_roles(path=PORT_ROLE_CACHE_FILE):
    """Read the role -> port hints written by :func:`save_port_roles`.

    :return: ``{role: port}``; empty if the file is missing or unreadable
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, ValueError):
        return {}


def save_port_roles(roles, path=PORT_ROLE_CACHE_FILE):
    """Persist the role -> port hints, merged over whatever is already stored.

    Roles that resolved to None are dropped rather than cached as misses.

    :param roles: ``{role: port or None}``
    """
    merged = load_port_roles(path)
    merged.update({k: v for k, v in roles.items() if v})
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2, sort_keys=True)
    except OSError as exc:
        logger.debug("Could not write port role cache %s: %s", path, exc)


def discover_roles(specs, timeout=4.0, hint_timeout=1.5, use_cache=True,
                   cache_path=PORT_ROLE_CACHE_FILE, verbose=False):
    """Resolve ``{role: (question, answer)}`` to ``{role: port}``, cache-first.

    Confirms the ports remembered from the previous run with a short timeout,
    then does one full :func:`find_devices` pass for whatever is still missing,
    and writes the result back to the cache.

    :param specs: ``{role: (question, answer)}`` as for :func:`find_devices`
    :param timeout: per-port timeout for the full scan
    :param hint_timeout: per-port timeout when re-checking a cached port; a
        device that is still there answers within one or two polls
    :param use_cache: set False to ignore (but still update) the cache
    :return: ``{role: port or None}``
    """
    found = {}
    remaining = dict(specs)

    if use_cache:
        hints = load_port_roles(cache_path)
        present = set(serial_ports())
        # Probe one cached port at a time, asking only for the role it held: a
        # port that still hosts the same instrument confirms almost instantly.
        for role in list(remaining):
            port = hints.get(role)
            if port is None or port not in present or port in found.values():
                continue
            hit = find_devices({role: remaining[role]}, timeout=hint_timeout,
                               ports=[port], verbose=verbose)
            if hit.get(role):
                found[role] = hit[role]
                del remaining[role]
        if found:
            logger.info("Confirmed from cache: %s",
                        ", ".join(f"{r}={p}" for r, p in sorted(found.items())))

    if remaining:
        # Scan the ports we haven't already claimed.
        claimed = set(found.values())
        rest = [p for p in serial_ports() if p not in claimed]
        found.update(find_devices(remaining, timeout=timeout, ports=rest,
                                  verbose=verbose))

    save_port_roles(found, cache_path)
    return found


# ============================================================================
# Protocol Constants & Low-Level Helpers
# ============================================================================

BAUDRATE = 115200


class AmbitProto:
    """Wire protocol for the Ambit device."""
    HELLO       = "hello\r\n"
    HELLO_ACK   = b"NEW"
    REBOOT      = "reboot\n"
    GET_PAR_RAW = "get_par\n"
    GET_PAR_CAL = "PAR\n"
    SET_SPEC    = "set_spec, {coeff:.4f}\n"
    SET_ACT     = "set_act, {coeff:.4f}\n"
    SET_NAME    = "set_name,{name}\n"
    LED_RUN     = "arrun1,1,1,2,0,0,1,0,1,{led:d},1,\n, \n"
    SET_CURRENTS = "set_currents,{i620:d},{i720:d},{ir:d},\n"
    # one type-2 line (no IR reflect), far-red off, sample number / frequency
    # as hi,lo bytes, actinic setting, ambient channels at every point
    ARRUN2       = "arrun2,1,0,2,0,{nh:d},{nl:d},{fh:d},{fl:d},{act:d},1,\n, \n"


class MiniParProto:
    """Wire protocol for the MiniPAR device.

    The MiniPAR firmware answers in plain text unless JSON mode is requested
    (``handleCommandText(cmd, jsonMode=false)`` is the default), so every reply
    below is a single bare line.
    """
    GET_PAR_RAW   = "par_raw\n"       # -> "<float>"  PAR before slope/intercept
    GET_PAR_CAL   = "par\n"           # -> "<float>"  par_raw * slope + intercept
    GET_SPEC_RAW  = "spec_raw\n"      # -> "<model>,<raw counts...>"
    SPEC_STATUS   = "spec_status\n"   # -> "model=..,available=..,atime=..,astep=..,gain=.."
    GET_SPEC_COEF = "get_spec_coeff\n"  # -> 18 comma-separated per-channel PAR coefficients
    GET_CAL_PAR   = "get_cal_par\n"   # -> "slope=..,intercept=.."
    GET_NAME      = "get_name\n"
    SET_NAME      = "set_name,{name}\n"


class DCSourceProto:
    """Wire protocol for the Kiprim DC source."""
    SET_VOLTAGE = "voltage {v:.3f}\r\n"
    SET_CURRENT = "current {i:.3f}\r\n"
    IDN         = "*IDN?\n"


def _query(port, cmd, decode="utf-8"):
    """Open, flush, write, readline. Returns decoded+stripped response."""
    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        ser.write(cmd.encode())
        return ser.readline().decode(encoding=decode).strip()


def _command(port, cmd):
    """Open, flush, write. Fire-and-forget."""
    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        ser.write(cmd.encode())


def _ambit_query(port, cmd, decode="unicode_escape"):
    """Open, flush, readiness handshake, write, readline. For Ambit reads."""
    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        _wait_for_device_ready(ser)
        ser.write(cmd.encode())
        return ser.readline().decode(encoding=decode).strip()


def _ambit_query_lines(port, cmd, n_lines=2, timeout=2.0, decode="unicode_escape"):
    """Like _ambit_query, but read a multi-line response.

    Opens with a read timeout so a missing trailing line degrades to "" instead
    of blocking forever (e.g. firmware that doesn't print it).

    :return: list of n_lines decoded+stripped lines.
    """
    with serial.Serial(port, baudrate=BAUDRATE, timeout=timeout) as ser:
        ser.flush()
        _wait_for_device_ready(ser)
        ser.write(cmd.encode())
        return [ser.readline().decode(encoding=decode).strip()
                for _ in range(n_lines)]


def _open_ambit_serial(port, timeout=1):
    """Open the Ambit serial port WITHOUT triggering the device's auto-reset.

    The flasher bridge wires DTR/RTS to the ESP32-C3 reset/boot pins, so the
    usual "open then assert DTR/RTS" sequence reboots the device. That reboot
    is what switches the actinic LED off right after ``arrun`` latches it on
    (the ~100 ms "flash"). Setting DTR/RTS to a steady state *before* opening
    avoids the reset edge, so a latched LED stays lit after the call returns
    (verified: no boot log on open, close, or reopen).
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUDRATE
    ser.timeout = timeout
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def _ambit_command(port, cmd, settle=0.2, verify_ready=True):
    """Open, flush, readiness handshake, write, settle delay [, re-verify]. For Ambit writes."""
    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        _wait_for_device_ready(ser)
        ser.write(cmd.encode())
        if settle > 0:
            time.sleep(settle)
        if verify_ready:
            _wait_for_device_ready(ser)


def set_voltage(port, voltage):
    """Set voltage on DC source via serial port."""
    _command(port, DCSourceProto.SET_VOLTAGE.format(v=voltage))


def set_current(port, current):
    """Set current on DC source via serial port."""
    _command(port, DCSourceProto.SET_CURRENT.format(i=current))


# ============================================================================
# PAR Reading Functions
# ============================================================================

def get_par_MP(port, raw=False):
    """
    Read PAR value from MiniPAR device.

    :param port: Serial port of the MiniPAR device
    :param raw: If True, request raw PAR value; if False, request calibrated value
    :return: PAR value as float
    """
    cmd = MiniParProto.GET_PAR_RAW if raw else MiniParProto.GET_PAR_CAL
    return float(_query(port, cmd))


def get_spec_raw_MP(port):
    """
    Read the raw (unscaled) spectrometer channel counts from a MiniPAR device.

    Sends 'spec_raw'; the MiniPAR answers '<model>,<c0>,...,<c9>' with the
    channels in order F1_415..F8_680, CLEAR, NIR.

    :param port: Serial port of the MiniPAR device
    :return: {"model": str, "counts": [int, ...]}, or None if the firmware
        doesn't support the command / the reply doesn't parse.
    """
    resp = _query(port, MiniParProto.GET_SPEC_RAW)
    try:
        model, *counts = resp.split(",")
        if model.startswith("error") or not counts:
            raise ValueError(resp)
        return {"model": model, "counts": [int(c) for c in counts]}
    except ValueError:
        print(f"[spec_raw] MiniPAR raw spectrum unavailable (reply: {resp!r})")
        return None


def get_par_raw_MP(port):
    """Read the MiniPAR's *uncalibrated* PAR (before slope/intercept).

    :param port: Serial port of the MiniPAR device
    :return: PAR value as float, or None if the reply didn't parse
    """
    resp = _query(port, MiniParProto.GET_PAR_RAW)
    try:
        return float(resp)
    except ValueError:
        print(f"[par_raw] MiniPAR raw PAR unavailable (reply: {resp!r})")
        return None


def get_spec_status_MP(port):
    """Read the MiniPAR's live spectrometer acquisition settings.

    Needed to turn its raw counts into basic counts: the MiniPAR normalises by
    ``gain * integration_time``, so the divisor depends on atime/astep/gain
    rather than being a fixed number.

    :param port: Serial port of the MiniPAR device
    :return: ``{"model", "available", "atime", "astep", "gain"}``, or None
    """
    resp = _query(port, MiniParProto.SPEC_STATUS)
    kv = dict(tok.split("=", 1) for tok in resp.split(",") if "=" in tok)
    if "model" not in kv:
        print(f"[spec_status] MiniPAR status unavailable (reply: {resp!r})")
        return None
    out = {"model": kv["model"]}
    for key in ("available", "atime", "astep", "gain"):
        try:
            out[key] = int(kv[key])
        except (KeyError, ValueError):
            out[key] = None
    return out


def get_spec_coeff_MP(port):
    """Read the MiniPAR's 18 per-channel PAR coefficients.

    These are the authoritative PAR weights: reading them off the device means
    the "MiniPAR method" recomputed here always matches the firmware actually
    on the bench, instead of a table copied into this file that can drift.

    :param port: Serial port of the MiniPAR device
    :return: list of floats, or None if the reply didn't parse
    """
    resp = _query(port, MiniParProto.GET_SPEC_COEF)
    try:
        return [float(v) for v in resp.split(",")]
    except ValueError:
        print(f"[spec_coeff] MiniPAR coefficients unavailable (reply: {resp!r})")
        return None


def get_cal_par_MP(port):
    """Read the MiniPAR's PAR calibration slope / intercept.

    :param port: Serial port of the MiniPAR device
    :return: ``{"slope": float, "intercept": float}``, or None
    """
    resp = _query(port, MiniParProto.GET_CAL_PAR)
    kv = dict(tok.split("=", 1) for tok in resp.split(",") if "=" in tok)
    try:
        return {"slope": float(kv["slope"]), "intercept": float(kv["intercept"])}
    except (KeyError, ValueError):
        print(f"[cal_par] MiniPAR calibration unavailable (reply: {resp!r})")
        return None


def read_minipar_reference(port):
    """Snapshot everything needed to reproduce a MiniPAR PAR reading offline.

    :param port: Serial port of the MiniPAR device
    :return: ``{"spec_status", "par_coefficients", "calibration"}``
    """
    return {
        "spec_status":      get_spec_status_MP(port),
        "par_coefficients": get_spec_coeff_MP(port),
        "calibration":      get_cal_par_MP(port),
    }


# ============================================================================
# PAR maths - the MiniPAR method (the reference definition)
# ============================================================================
# The MiniPAR firmware (miniPar/Firmware/src/app/spectrometer_api.cpp) computes
#
#     basic_count[i] = raw[i] / (gain * (atime+1) * (astep+1) * 2.78e-6)
#     par_raw        = sum_i basic_count[i] * par_coefficients[i]
#     par            = par_raw * slope + intercept
#
# The two functions below are a line-for-line port, so a PAR value can be
# recomputed from stored raw counts and cross-checked against what the device
# reported. See AMBIT_PAR_DISCREPANCIES for how the Ambit differs.

AS7341_TSTEP_S = 2.78e-6   # AS7341/AS7343 integration step, seconds


def gain_reg_to_multiplier(gain_reg):
    """Convert an AS7341/AS7343 gain register value to its multiplier.

    Both parts use: reg 0 -> 0.5x, reg >= 1 -> 2^(reg-1).

    :param gain_reg: raw gain register value
    :return: gain multiplier as float
    """
    return 0.5 if gain_reg == 0 else float(1 << (gain_reg - 1))


def basic_count_divisor(gain_reg, atime, astep):
    """Return ``gain * integration_time``, the raw-count -> basic-count divisor.

    :param gain_reg: gain register value (see :func:`gain_reg_to_multiplier`)
    :param atime: ATIME register value
    :param astep: ASTEP register value
    :return: divisor as float
    """
    return (gain_reg_to_multiplier(gain_reg)
            * (atime + 1.0) * (astep + 1.0) * AS7341_TSTEP_S)


def par_from_counts(counts, coefficients, divisor):
    """PAR from raw spectrometer counts, the MiniPAR way.

    :param counts: raw per-channel counts, in the device's channel order
    :param coefficients: per-channel PAR coefficients, same order
    :param divisor: from :func:`basic_count_divisor`
    :return: PAR (in the coefficients' units), or None on missing input
    """
    if not counts or not coefficients or not divisor:
        return None
    return float(sum((c / divisor) * k
                     for c, k in zip(counts, coefficients)))


# ---------------------------------------------------------------------------
# Ambit spectrometer: fixed acquisition, integer-scaled channels
# ---------------------------------------------------------------------------
# ambit-iot/src/src/as7341/spec_meas.cpp::get_PAR() always measures at
# dual_exposure(AS7341_GAIN_2X, ...) with ATIME=99 / ASTEP=499, reports each
# channel pre-multiplied by an integer Spec_COE, and sums them with a single
# flat weight. The channel order is F1..F8 then NIR then CLEAR - note NIR before
# CLEAR, the opposite of the MiniPAR.
AMBIT_GAIN_REG = 2      # AS7341_GAIN_2X
AMBIT_ATIME    = 99
AMBIT_ASTEP    = 499
AMBIT_SPEC_COE = [12, 10, 11, 10, 10, 9, 7, 4, 1, 1]   # Spec_COE1..9 (+CLEAR unscaled)
AMBIT_SPEC_CHANNELS = ["f1_415", "f2_445", "f3_480", "f4_515", "f5_555",
                       "f6_590", "f7_630", "f8_680", "nir", "clear"]
MINIPAR_AS7341_CHANNELS = ["f1_415", "f2_445", "f3_480", "f4_515", "f5_555",
                           "f6_590", "f7_630", "f8_680", "clear", "nir"]

#: Human-readable audit of how the Ambit's PAR differs from the MiniPAR method.
#: Recorded in the calibration payload so a stored calibration carries the
#: caveats of the firmware that produced it.
AMBIT_PAR_DISCREPANCIES = [
    "normalisation: MiniPAR divides by gain*integration_time (basic counts), so its "
    "PAR is independent of atime/astep/gain. The Ambit uses raw counts at a hardwired "
    "GAIN_2X/ATIME=99/ASTEP=499 and never normalises.",
    "spectral weights: MiniPAR applies a per-channel float coefficient. The Ambit "
    "multiplies each channel by an integer Spec_COE {12,10,11,10,10,9,7,4,1,1} and then "
    "sums channels F1..F8 with one flat 0.006 weight, minus NIR*0.0075, times PAR_OFFSET=4. "
    "Effective Ambit weights are {0.288,0.24,0.264,0.24,0.24,0.216,0.168,0.096} for F1..F8 "
    "versus MiniPAR's {1.095,0.094,0.197,0.165,0.187,0.130,0.169,0.064}: F1_415 is ~3.8x "
    "under-weighted and F2_445 ~2.5x over-weighted.",
    "CLEAR channel: MiniPAR subtracts it (coefficient -0.108). The Ambit ignores CLEAR "
    "entirely and only subtracts NIR.",
    "overflow: the Ambit stores channel*Spec_COE in uint16, so any channel whose scaled "
    "value exceeds 65535 wraps. F1_415 wraps above ~5461 raw counts.",
    "consequence: the two PAR definitions are not related by a single scale factor, so a "
    "one-slope fit of Ambit-raw against the MiniPAR reference is only valid for the "
    "spectrum of the calibration lamp. Fixing this needs a firmware change in "
    "ambit-iot/src/src/as7341/spec_meas.cpp; this script records the mismatch it sees.",
]


def ambit_spec_unscale(spec):
    """Recover the Ambit's raw spectrometer counts from its reported channels.

    The Ambit reports ``raw * Spec_COE`` truncated to uint16, so dividing by
    Spec_COE inverts it - except where the product wrapped. A wrap is detected
    by divisibility: an unwrapped channel is an exact multiple of its Spec_COE.
    That test cannot see a wrap where Spec_COE divides 65536 (F8_680 with COE 4,
    and NIR / CLEAR with COE 1), so those are reported as unknown rather than
    clean.

    :param spec: the 10 channel values from :func:`get_par_AMB`
    :return: ``(raw_counts, wrapped_flags)``; ``wrapped_flags[i]`` is True when a
        wrap was detected, False when the channel is provably clean, and None
        where the test can't tell. ``(None, None)`` if ``spec`` is unusable.
    """
    if not spec or len(spec) != len(AMBIT_SPEC_COE):
        return None, None
    raw, wrapped = [], []
    for value, coe in zip(spec, AMBIT_SPEC_COE):
        raw.append(value / coe)
        if 65536 % coe == 0:          # divisibility carries no information here
            wrapped.append(None)
        else:
            wrapped.append(value % coe != 0)
    return raw, wrapped


def ambit_par_minipar_method(spec, coefficients):
    """Recompute the Ambit's PAR using the MiniPAR definition.

    Un-scales the Ambit's channels (see :func:`ambit_spec_unscale`), reorders
    them from the Ambit's F1..F8,NIR,CLEAR to the MiniPAR's F1..F8,CLEAR,NIR,
    and applies the MiniPAR coefficients at the Ambit's fixed acquisition
    settings. Diagnostic only: it quantifies the spectral-weighting mismatch,
    it does not correct it (the firmware still reports its own PAR).

    :param spec: the 10 channel values from :func:`get_par_AMB`
    :param coefficients: MiniPAR per-channel coefficients from
        :func:`get_spec_coeff_MP` (only the first 10 are used)
    :return: PAR in MiniPAR units, or None if it can't be computed
    """
    raw, _wrapped = ambit_spec_unscale(spec)
    if raw is None or not coefficients:
        return None
    by_name = dict(zip(AMBIT_SPEC_CHANNELS, raw))
    ordered = [by_name[name] for name in MINIPAR_AS7341_CHANNELS]
    divisor = basic_count_divisor(AMBIT_GAIN_REG, AMBIT_ATIME, AMBIT_ASTEP)
    return par_from_counts(ordered, coefficients[:len(ordered)], divisor)


def get_par_AMB(port, raw=False, return_spec=False):
    """
    Read PAR value from Ambit device.

    The firmware answers 'get_par'/'PAR' with two lines: the PAR value, then
    the 10 spectrometer channel values (F1_415..F8_680, NIR, CLEAR) as CSV.
    Both lines are always read so the serial buffer stays clean; the channel
    values are pre-scaled by the firmware Spec_COE factors and wrap at uint16.

    :param port: Serial port of the Ambit device
    :param raw: If True, request raw PAR value; if False, request calibrated value
    :param return_spec: If True, also return the spectrometer channel values
    :return: PAR value as float, or (par, channels) if return_spec=True where
        channels is a list of 10 ints (None if the channel line didn't parse)
    """
    cmd = AmbitProto.GET_PAR_RAW if raw else AmbitProto.GET_PAR_CAL
    par_line, spec_line = _ambit_query_lines(port, cmd, n_lines=2)
    par = float(par_line)
    if not return_spec:
        return par
    try:
        spec = [int(v) for v in spec_line.split(",")]
    except ValueError:
        print(f"[get_par] Ambit channel line didn't parse (reply: {spec_line!r})")
        spec = None
    return par, spec


def record_arrun_AMB(port, actinic=0, num_points=5, freq=10, timeout=15.0):
    """
    Record an ADPD array run on the Ambit and return the parsed data arrays.

    Sends 'set_currents,0,0,0' (ADPD pulse LEDs dark, so the detector records
    only the incident light) followed by an 'arrun2' trace, both over a single
    port-open session: opening the port resets the device, so a separate open
    would undo the zeroed currents. Note the run drives the actinic LED per
    ``actinic`` (firmware forces it OFF when <= 3) and leaves it off afterwards.

    The device replies with one 'Data:<tag>,Length:N\\t<v>,<v>,...' line per
    channel buffer (env, s_630, r_630, sun, leaf, s_730, r_730) and a final
    'Data sent'.

    :param port: Serial port of the Ambit device
    :param actinic: actinic LED setting driven during the run (0 = off)
    :param num_points: samples to record
    :param freq: sampling frequency in Hz
    :param timeout: overall seconds to wait for the data dump
    :return: {"actinic", "num_points", "freq_hz", "data": {tag: [ints]}},
        or None if no data arrived.
    """
    nh, nl = divmod(int(num_points), 256)
    fh, fl = divmod(int(freq), 256)
    data, got_end = {}, False
    with serial.Serial(port, baudrate=BAUDRATE, timeout=2.0) as ser:
        ser.flush()
        _wait_for_device_ready(ser)

        ser.write(AmbitProto.SET_CURRENTS.format(i620=0, i720=0, ir=0).encode())
        echo = ser.readline()
        if b"Currents set" not in echo:
            print(f"[arrun] unexpected set_currents echo: {echo!r}")

        ser.write(AmbitProto.ARRUN2.format(nh=nh, nl=nl, fh=fh, fl=fl,
                                           act=int(actinic)).encode())
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("Data:"):
                head, _, values = line.partition("\t")
                tag = head[len("Data:"):].split(",", 1)[0]
                try:
                    data[tag] = [int(v) for v in values.split(",") if v.strip()]
                except ValueError:
                    data[tag] = values          # keep unparseable payload as text
            elif "Data sent" in line:
                got_end = True
                break

    if not got_end:
        print(f"[arrun] 'Data sent' not received within {timeout}s "
              f"(got tags: {sorted(data)})")
    if not data:
        return None
    return {"actinic": int(actinic), "num_points": int(num_points),
            "freq_hz": int(freq), "data": data}


#: ADPD channels an arrun2 run fills. 's_730'/'r_730' stay empty because the
#: run is issued as type 2 (no IR reflect); 'sun'/'leaf' are populated only
#: because sub-sampling is 1 (ambient sampled at every point).
ARRUN_CHANNELS = ("env", "s_630", "r_630", "sun", "leaf")


def summarize_arrun(arrun, channels=ARRUN_CHANNELS):
    """Reduce one :func:`record_arrun_AMB` result to per-channel statistics.

    :param arrun: an arrun record, or None
    :param channels: channel tags to summarise
    :return: ``{tag: {"n", "mean", "std", "min", "max"}}`` for every requested
        channel that holds numeric samples; ``{}`` if there is nothing to reduce
    """
    if not arrun or not arrun.get("data"):
        return {}
    out = {}
    for tag in channels:
        values = arrun["data"].get(tag)
        if not values or not all(isinstance(v, (int, float)) for v in values):
            continue
        arr = np.asarray(values, dtype=float)
        out[tag] = {
            "n":    int(arr.size),
            "mean": float(arr.mean()),
            "std":  float(arr.std(ddof=1)) if arr.size > 1 else 0.0,
            "min":  float(arr.min()),
            "max":  float(arr.max()),
        }
    return out


# ============================================================================
# Calibration Functions
# ============================================================================

def _wait_for_device_ready(ser, expected_response=AmbitProto.HELLO_ACK, max_retries=10):
    """
    Wait for device to be ready by polling with 'hello' command.

    :param ser: Serial port object
    :param expected_response: Byte string to look for in response
    :param max_retries: Maximum number of retry attempts
    :return: The response received from device
    """
    resp = b""
    for _ in range(max_retries):
        ser.write(AmbitProto.HELLO.encode())
        resp = ser.readline()
        if expected_response in resp:
            return resp
        time.sleep(0.1)
    return resp


def set_par_gain(port, coeff):
    """
    Upload PAR calibration coefficient (slope) to Ambit device.

    :param port: Serial port of the Ambit device
    :param coeff: Calibration coefficient value
    """
    _ambit_command(port, AmbitProto.SET_SPEC.format(coeff=coeff))


def set_ambit_led_gain(port, coeff):
    """
    Set LED calibration gain on Ambit device.

    :param port: Serial port of the Ambit device
    :param coeff: Calibration coefficient value
    """
    _ambit_command(port, AmbitProto.SET_ACT.format(coeff=coeff))




# ============================================================================
# Device Information & Management
# ============================================================================

@dataclass
class AmbitInfo:
    """Container for Ambit device information parsed from a reboot dump."""

    # Identity / firmware
    FW: bytes = b""                                    # e.g. b"0.0.4"
    IsValid: bool = False
    name: bytes = b""                                  # calibration "Name", e.g. b"AmbitV004"

    # Firmware metadata
    MAC: str = ""
    fw_size: int = 0
    fw_date: str = ""

    # Chip detection
    adpd_chip_version: "int | None" = None

    # Metadata snapshot (GPS + IMU)
    metadata: dict = field(default_factory=dict)       # lon/lat/alt/time/acc/vacc/info1/x/y/z

    # Main calibration line
    act_led_coeff: float = 0.0                         # Actinic
    light_slope: float = 0.0                           # Spec
    emit_coeff: float = 0.0
    sun_coeff: float = 0.0
    temp_offset: float = 0.0
    temp_slope: float = 0.0

    # Actinic LED curve {50: 983, 100: 2032, 150: 3121, 200: 4174, 250: 5233}
    actinic_curve: dict = field(default_factory=dict)

    # ADPD + MLX raw calibration vectors
    adpd_calibration: list = field(default_factory=list)
    mlx_calibration: list = field(default_factory=list)

    def processInfo(self, line):
        try:
            text = line.decode(errors="replace").strip()
        except Exception:
            return
        if not text:
            return

        if "ADPD Found" in text and "chip version:" in text:
            try:
                self.adpd_chip_version = int(text.split("chip version:")[1].strip())
            except ValueError:
                pass
            return

        if text.startswith("Metadata:"):
            self.metadata = {
                k: _coerce_num(v)
                for k, v in _kv_pairs(text[len("Metadata:"):]).items()
            }
            return

        if text.startswith("Calibration:"):
            payload = text[len("Calibration:"):].strip()

            # "ADPD: 0\t0\t0\t0\t0\t0"
            if payload.startswith("ADPD"):
                _, vals = payload.split(":", 1)
                self.adpd_calibration = [_coerce_num(v) for v in vals.split()]
                return

            kv = _kv_pairs(payload)

            # Act_50, Act_100, ...  -> {50: 983, ...}
            curve = {int(k.split("_")[1]): int(v)
                     for k, v in kv.items() if k.startswith("Act_")}
            if curve:
                self.actinic_curve.update(curve)
                return

            if "Name" in kv:        self.name = kv["Name"].encode()
            if "Actinic" in kv:     self.act_led_coeff = float(kv["Actinic"])
            if "Spec" in kv:
                self.light_slope = float(kv["Spec"])
                self.IsValid = True
            if "Emit" in kv:        self.emit_coeff = float(kv["Emit"])
            if "Sun" in kv:         self.sun_coeff = float(kv["Sun"])
            if "Temp_offset" in kv: self.temp_offset = float(kv["Temp_offset"])
            if "Temp_slope" in kv:  self.temp_slope = float(kv["Temp_slope"])
            return

        if text.startswith("MLX:"):
            self.mlx_calibration = [_coerce_num(v)
                                    for v in text[len("MLX:"):].split() if v]
            return

        if text.startswith("FW:"):
            body = text[len("FW:"):].strip()
            if "MAC:" in body:
                # Tab-separated; the Date value contains spaces ("Mar  5 2026"),
                # so split on tabs only rather than on any whitespace.
                kv = _kv_pairs(body, sep="\t")
                self.MAC = kv.get("MAC", "")
                self.fw_size = int(kv["Size"]) if kv.get("Size", "").isdigit() else 0
                self.fw_date = kv.get("Date", "")
            else:
                self.FW = body.encode()
                self.IsValid = True
            return

    def to_dict(self):
        """Return all parsed device info as a plain (JSON-friendly) dict.

        Byte fields (``FW``, ``name``) are decoded to ``str`` and the nested
        ``metadata`` / ``actinic_curve`` dicts and calibration lists are copied
        so the result can be mutated without touching this instance.
        """
        return {
            "FW": self.FW.decode(errors="replace"),
            "IsValid": self.IsValid,
            "name": self.name.decode(errors="replace"),
            "MAC": self.MAC,
            "fw_size": self.fw_size,
            "fw_date": self.fw_date,
            "adpd_chip_version": self.adpd_chip_version,
            "act_led_coeff": self.act_led_coeff,
            "light_slope": self.light_slope,
            "emit_coeff": self.emit_coeff,
            "sun_coeff": self.sun_coeff,
            "temp_offset": self.temp_offset,
            "temp_slope": self.temp_slope,
            "actinic_curve": dict(self.actinic_curve),
            "adpd_calibration": list(self.adpd_calibration),
            "mlx_calibration": list(self.mlx_calibration),
            "metadata": dict(self.metadata),
        }

    def __str__(self):
        return (
            f"FW: {self.FW} (MAC={self.MAC}, size={self.fw_size}B, date={self.fw_date})\n"
            f"Name: {self.name}, valid: {self.IsValid}\n"
            f"Calibration: Spec(light_slope)={self.light_slope}, "
            f"Actinic(act_led_coeff)={self.act_led_coeff}, "
            f"Emit={self.emit_coeff}, Sun={self.sun_coeff}, "
            f"Temp_offset={self.temp_offset}, Temp_slope={self.temp_slope}\n"
            f"Actinic curve: {self.actinic_curve}\n"
            f"ADPD cal: {self.adpd_calibration} (chip v{self.adpd_chip_version})\n"
            f"MLX cal: {self.mlx_calibration}\n"
            f"Metadata: {self.metadata}"
        )


def _kv_pairs(text, sep=None):
    """Parse 'k:v<sep>k:v ...' into a dict.

    With the default sep=None, splits on any run of whitespace and also treats
    commas as separators. Pass sep="\\t" to split on tabs only, which preserves
    values that contain spaces (e.g. a 'Date:Mar  5 2026' field).
    """
    out = {}
    tokens = text.split(sep) if sep is not None else text.replace(",", " ").split()
    for tok in tokens:
        tok = tok.strip()
        if ":" in tok:
            k, v = tok.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def _coerce_num(v):
    try:
        if "." in v: return float(v)
        return int(v)
    except (ValueError, TypeError):
        return v



def ambit_reboot(port):
    """
    Reboot Ambit device and retrieve its configuration information.

    :param port: Serial port of the Ambit device
    :return: AmbitInfo object with device configuration
    """
    info = AmbitInfo()

    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        ser.write(AmbitProto.HELLO.encode())
        resp = ser.readline()
        ser.write(AmbitProto.HELLO.encode())
        resp = ser.readline()

        while AmbitProto.HELLO_ACK not in resp:
            ser.write(AmbitProto.HELLO.encode())
            resp = ser.readline()

        ser.write(AmbitProto.REBOOT.encode())

        # Process ambit data
        for i in range(26):
            l = ser.readline()
            info.processInfo(l)
            logger.debug("ambit boot line: %s", l)
            if b"FW:" in l and b"MAC" not in l:
                info.IsValid = True
                break

    # Verify device is back online
    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        ser.write(AmbitProto.HELLO.encode())
        r = ser.readline()
        ser.write(AmbitProto.HELLO.encode())
        r = ser.readline()

    return info


def set_ambit_name(port, name):
    """
    Set the Ambit device name.

    Sends ``hello`` twice, waits for the device's acknowledgment, then sends
    ``set_name,<name>``.

    :param port: Serial port of the Ambit device
    :param name: New device name (string)
    """
    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        ser.write(AmbitProto.HELLO.encode())
        resp = ser.readline()
        ser.write(AmbitProto.HELLO.encode())
        resp = ser.readline()

        while AmbitProto.HELLO_ACK not in resp:
            ser.write(AmbitProto.HELLO.encode())
            resp = ser.readline()

        ser.write(AmbitProto.SET_NAME.format(name=name).encode())
        time.sleep(0.2)


def set_MP_name(port, name="miniPAR", verbose=False):
    """
    Set the device name on a MiniPAR device.

    Sends ``set_name,<name>`` and verifies the device echoes the new name back
    in the ``device_name`` field of its JSON response.

    :param port: Serial port of the MiniPAR device
    :param name: New device name (default: "miniPAR")
    :param verbose: If True, log the raw device response
    :return: True if the device confirmed the new name, False otherwise
    """
    resp = _query(port, MiniParProto.SET_NAME.format(name=name))
    if verbose:
        logger.info("Response from device: %s", resp)
    try:
        returned_name = json.loads(resp).get("device_name", "")
    except json.JSONDecodeError:
        logger.warning("Could not parse MiniPAR response: %s", resp)
        return False
    if returned_name != name:
        logger.warning("Error setting name %r: device returned %r", name, returned_name)
        return False
    return True


# Fields of AmbitInfo.to_dict() that are dropped from the uploaded payload.
#   metadata - the boot dump's GPS/IMU block is placeholder data on the bench
#              (lon/lat/alt all 1.0, x/y/z all 0.0, info1 "New_Ambit"); the Ambit
#              has no fix indoors, so it records nothing about the calibration.
#   IsValid  - an artefact of parsing the boot dump, not a device property; a
#              payload only ever gets built once both dumps parsed.
CALIBRATION_DROPPED_FIELDS = ("metadata", "IsValid")


def _device_dict(info):
    """AmbitInfo.to_dict() minus the fields in CALIBRATION_DROPPED_FIELDS."""
    return {k: v for k, v in info.to_dict().items()
            if k not in CALIBRATION_DROPPED_FIELDS}


def make_calibration_payload(info_precalibration=None, info_postcalibration=None, *,
                             device_id=None, device_name=None,
                             firmware_version=None, device_firmware=None,
                             device_version="1", protocol_id="CALIBRATION",
                             par_cal=None, led_cal=None, station=None,
                             indent=None):
    """Build the openJII calibration-upload payload from the pre/post AmbitInfo dumps.

    Payload shape follows openJII's ``sensor_schema``
    (open-jii/apps/data/src/lib/openjii/openjii/centrum/schemas.py), where
    ``sample`` is declared ``StringType``. It must therefore be a JSON *string*,
    not a nested array: ``from_json`` yields null for a mismatched type, which
    silently drops the whole calibration on ingest. The topic, client id and
    ingestion timestamps are added by the AWS IoT rule, so they are not sent.

    The device dump is stored once, as the post-calibration state, plus a
    ``device_before`` diff holding only the fields the run actually changed -
    the two full dumps used to be near-identical, differing in the device name
    and (when calibration ran) two coefficients.

    The ``device_*`` / ``firmware_version`` fields default to values read from
    ``info_postcalibration`` (falling back to ``info_precalibration``); pass
    explicit strings to override any of them.

    :param info_precalibration: AmbitInfo captured before calibration
    :param info_postcalibration: AmbitInfo captured after calibration
    :param par_cal: PAR-sensor calibration block from calibrate_par_sensor();
        ``None`` (-> JSON null) if it was skipped
    :param led_cal: actinic-LED calibration block from calibrate_led();
        ``None`` if it was skipped
    :param station: optional bench/provenance block (firmware release flashed,
        reference-instrument settings, ...)
    :param indent: json.dumps indent; None keeps the payload compact, which
        matters against the 128 KB AWS IoT message limit
    :return: a JSON string ready to send
    :raises ValueError: if either AmbitInfo is missing / never populated
    """
    empty = [
        label for label, info in (("info_precalibration", info_precalibration),
                                  ("info_postcalibration", info_postcalibration))
        if info is None or not getattr(info, "IsValid", False)
    ]
    if empty:
        logger.warning(
            "make_calibration_payload aborted: %s %s empty / not populated - "
            "call ambit_reboot() to fill them before building the payload.",
            " and ".join(empty), "are" if len(empty) > 1 else "is",
        )
        raise ValueError(f"Cannot build calibration payload: {', '.join(empty)} empty / not populated")

    def _pick(attr, default=""):
        for src in (info_postcalibration, info_precalibration):
            v = getattr(src, attr, None)
            if v:
                return v.decode(errors="replace") if isinstance(v, bytes) else str(v)
        return default

    mac  = device_id        or _pick("MAC")        or "MACID"
    name = device_name      or _pick("name")       or "NAME"
    fw   = firmware_version or _pick("FW")          or "1"

    device = _device_dict(info_postcalibration)
    before = _device_dict(info_precalibration)
    changed = {k: v for k, v in before.items() if device.get(k) != v}

    sample = [
        {
            "protocol_id": protocol_id,
            "set": [
                {
                    "device":         device,
                    "device_before":  changed or None,
                    "par_sensor_calibration": par_cal,
                    "led_calibration":        led_cal,
                    "station":                station,
                }
            ],
        }
    ]

    payload = {
        # openJII reads sample as a JSON string; keep it compact.
        "sample": json.dumps(sample, separators=(",", ":")),
        "device_firmware": device_firmware or fw,
        "device_id": mac,
        "device_name": name,
        "device_version": device_version,
        "firmware_version": fw,
        "timestamp": iso_timestamp(),
    }
    return json.dumps(payload, indent=indent)


# ============================================================================
# Firmware Flashing (Ambit)
# ============================================================================
# Self-contained Ambit firmware flasher ported from the standalone
# ``ambit_uploader.py``: it locates the compiled firmware images, resolves
# esptool, finds the flasher serial port, and runs the flash - so the
# calibration tooling can (re)flash an Ambit without shelling out to any
# external uploader script.

# WCH CH343 USB-serial bridge used by the Ambit flasher.
FLASHER_VID = 0x1A86
FLASHER_PID = 0x55D4
FLASHER_VIDPID = "1A86:55D4"

# Compiled Ambit firmware images; all must live together in one folder.
AMBIT_FIRMWARE_FILES = [
    "ambit-1.ino.bin",
    "ambit-1.ino.bootloader.bin",
    "ambit-1.ino.partitions.bin",
    "boot_app0.bin",
]

# esptool flash offset -> image, for the ESP32-C3 on the Ambit. Used for a
# hand-placed Arduino-IDE export; a GitHub release carries its own layout in
# manifest.json (see fetch_ambit_release).
_AMBIT_FLASH_LAYOUT = [
    ("0x0",     "ambit-1.ino.bootloader.bin"),
    ("0x8000",  "ambit-1.ino.partitions.bin"),
    ("0xe000",  "boot_app0.bin"),
    ("0x10000", "ambit-1.ino.bin"),
]


# ============================================================================
# Firmware releases (GitHub)
# ============================================================================
# ambit-iot publishes every firmware build as a GitHub release
# (.github/workflows/release.yml -> semantic-release). Each release carries the
# four images plus a manifest.json that states the chip, the version and the
# flash offsets, so the flasher never has to hardcode a layout or a filename:
#
#   {"name": "ambit-iot", "version": "1.1.0", "chip": "esp32c3",
#    "flash": [{"file": "bootloader.bin", "offset": "0x0", "size": .., "sha256": ..}, ...],
#    "ota": {"file": "ambit-fw-v1.1.0.bin"}}

AMBIT_FW_REPO = "Jan-IngenHousz-Institute/ambit-iot"
AMBIT_RELEASE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "firmware_ambit", "releases")


def _gh(*args, check=True):
    """Run the GitHub CLI and return its stdout.

    ``gh`` is used rather than raw HTTPS because the ambit-iot repository is
    private: the CLI already holds the operator's credentials, so no token has
    to be configured here.

    :raises RuntimeError: if gh is missing or the call fails
    """
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "the GitHub CLI ('gh') is required to download Ambit firmware releases - "
            "install it from https://cli.github.com and run 'gh auth login'"
        ) from exc
    if check and result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _sha256(path):
    """Return the hex SHA-256 of a file, read in chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _release_is_complete(directory, manifest):
    """True when every image in ``manifest`` is present in ``directory`` and its
    SHA-256 matches, i.e. a previous download can be reused as-is.
    """
    for entry in manifest.get("flash", []):
        path = os.path.join(directory, entry["file"])
        if not os.path.exists(path):
            return False
        if entry.get("sha256") and _sha256(path) != entry["sha256"]:
            logger.warning("cached %s has the wrong checksum - re-downloading", entry["file"])
            return False
    return True


def fetch_ambit_release(tag=None, repo=AMBIT_FW_REPO, cache_dir=AMBIT_RELEASE_CACHE):
    """Download an Ambit firmware release and return everything needed to flash it.

    Resolves the latest release (or ``tag``), downloads the assets into
    ``<cache_dir>/<tag>/``, and verifies each image against the SHA-256 in
    manifest.json. A release already sitting in the cache with matching
    checksums is reused, so only the first device of a session pays for the
    download.

    :param tag: release tag to fetch; None means the latest release
    :param repo: ``owner/name`` of the firmware repository
    :param cache_dir: where downloaded releases are kept
    :return: ``{"version", "tag", "dir", "chip", "layout"}`` where ``layout`` is
        the ``[(offset, filename), ...]`` list to hand to :func:`flash_ambit`
    :raises RuntimeError: if gh is unavailable, the release has no manifest, or
        a downloaded image fails its checksum
    """
    if tag is None:
        tag = json.loads(_gh("api", f"repos/{repo}/releases/latest",
                             "--jq", "{tag_name: .tag_name}"))["tag_name"]
        logger.info("Latest %s firmware release: %s", repo, tag)

    directory = os.path.join(cache_dir, tag)
    os.makedirs(directory, exist_ok=True)
    manifest_path = os.path.join(directory, "manifest.json")

    manifest = None
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except ValueError:
            manifest = None

    if manifest is None or not _release_is_complete(directory, manifest):
        logger.info("Downloading firmware %s into %s", tag, directory)
        _gh("release", "download", tag, "--repo", repo, "--dir", directory, "--clobber")
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    else:
        logger.info("Reusing cached firmware %s from %s", tag, directory)

    entries = manifest.get("flash")
    if not entries:
        raise RuntimeError(f"release {tag} manifest.json has no 'flash' section")

    for entry in entries:
        path = os.path.join(directory, entry["file"])
        if not os.path.exists(path):
            raise RuntimeError(f"release {tag} is missing image {entry['file']}")
        if entry.get("sha256") and _sha256(path) != entry["sha256"]:
            raise RuntimeError(f"checksum mismatch on {entry['file']} of release {tag}")

    return {
        "version": manifest.get("version") or tag.lstrip("v"),
        "tag":     tag,
        "dir":     directory,
        "chip":    manifest.get("chip", "esp32c3"),
        "layout":  [(entry["offset"], entry["file"]) for entry in entries],
    }


def find_file(start_dir, filename):
    """Return the path to the first ``filename`` found in ``start_dir`` or any
    of its sub-folders, or None if it is nowhere to be found.
    """
    for root, _dirs, files in os.walk(start_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None


def find_firmware_dir(start_dir, required_files=AMBIT_FIRMWARE_FILES):
    """Search ``start_dir`` and its sub-folders for a folder that holds every
    file in ``required_files``.

    :param start_dir: directory tree to search
    :param required_files: filenames that must all be present together
    :return: absolute path of the first matching folder, or None
    """
    required = set(required_files)
    for root, _dirs, files in os.walk(start_dir):
        if required.issubset(files):
            return os.path.abspath(root)
    return None


def esptool_command(firmware_dir=None):
    """Return the argv prefix used to invoke esptool, cross-platform.

    Prefers a bundled ``esptool.exe`` on Windows (searched inside
    ``firmware_dir`` when given); otherwise runs the installed ``esptool``
    package via ``python -m esptool``.

    :param firmware_dir: optional folder to search for a bundled esptool.exe
    :raises RuntimeError: if no esptool is available
    """
    if os.name == "nt" and firmware_dir:
        local_exe = find_file(firmware_dir, "esptool.exe")
        if local_exe:
            return [local_exe]
    if importlib.util.find_spec("esptool") is not None:
        return [sys.executable, "-m", "esptool"]
    raise RuntimeError(
        "esptool not found - install it with `pip install esptool`, "
        "or place esptool.exe next to the firmware images (Windows only)."
    )


def flasher_ports():
    """List serial ports that look like an Ambit flasher (WCH CH343 bridge).

    :return: list of port device names matching the flasher VID/PID
    """
    found = []
    for port in sorted(serial.tools.list_ports.comports()):
        try:
            device = getattr(port, "device", None)
            if not device:
                continue
            hwid = (getattr(port, "hwid", "") or "").upper()
            vidpid_ok = ((getattr(port, "vid", None), getattr(port, "pid", None))
                         == (FLASHER_VID, FLASHER_PID))
            if vidpid_ok or FLASHER_VIDPID in hwid:
                found.append(device)
        except Exception:
            continue
    return found


def detect_invalid_header(port, timeout_s=5, baud=BAUDRATE):
    """Listen briefly on ``port`` for the boot-time "invalid header" string.

    :param port: serial port to probe
    :param timeout_s: how long to listen, in seconds
    :return: (found, serial_output) - ``found`` is True if "invalid header" was seen
    """
    serial_output = ""
    logger.info("Listening on %s for %ss to detect boot status...", port, timeout_s)
    try:
        with serial.Serial(port, baud, timeout=0.1) as ser:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                chunk = ser.read(128)
                if not chunk:
                    continue
                serial_output += chunk.decode(errors="replace")
                if "invalid header" in serial_output:
                    return True, serial_output
    except serial.SerialException as exc:
        logger.error("Could not open serial port %s: %s", port, exc)
    return False, serial_output


def flash_ambit(port, firmware_dir, layout=None, chip="esp32c3"):
    """Write the Ambit firmware images in ``firmware_dir`` to the device on
    ``port`` by invoking esptool.

    The snake_case esptool options below are the deprecated spelling in esptool
    v5, but v5 still accepts them, so one command line works with both the
    bundled v4 esptool.exe and a pip-installed v5.

    :param port: serial port of the Ambit flasher
    :param firmware_dir: folder holding the images
    :param layout: ``[(offset, filename), ...]``; defaults to the hand-placed
        Arduino export layout. Pass the ``layout`` from
        :func:`fetch_ambit_release` to flash a GitHub release.
    :param chip: esptool target chip
    :raises RuntimeError: if esptool exits non-zero
    """
    cmd = [
        *esptool_command(firmware_dir),
        "--chip", chip,
        "--baud", "921600",
        "--port", port,
        "--before", "default_reset",
        "--after", "hard_reset",
        "write_flash", "-z",
        "--flash_mode", "keep",
        "--flash_freq", "keep",
        "--flash_size", "keep",
    ]
    for offset, image in (layout or _AMBIT_FLASH_LAYOUT):
        cmd += [offset, image]

    logger.info("Flashing %s with esptool...", port)
    result = subprocess.run(cmd, cwd=firmware_dir)
    if result.returncode != 0:
        raise RuntimeError(f"esptool exited with return code {result.returncode}")
    logger.info("Flash completed.")


def flash_ambit_firmware(firmware_dir=None, *, search_root=None, port=None,
                         force_flash=True, layout=None, chip="esp32c3"):
    """Locate the Ambit firmware, find the flasher port, and flash the device.

    Self-contained equivalent of the standalone ``ambit_uploader.py`` script:
    the calibration tooling can (re)flash an Ambit on its own.

    :param firmware_dir: folder holding the firmware images; if None, it is
        discovered by searching ``search_root`` (see :func:`find_firmware_dir`)
    :param search_root: directory tree searched for the firmware when
        ``firmware_dir`` is None (default: this module's own folder)
    :param port: flasher serial port; if None, auto-detected (exactly one
        flasher must be connected)
    :param force_flash: when True, always flash; when False, flash only if a
        boot-time "invalid header" is detected on the device
    :param layout: ``[(offset, filename), ...]`` to flash; defaults to the
        hand-placed Arduino export layout. Pass a release layout together with
        that release's ``dir`` as ``firmware_dir``.
    :param chip: esptool target chip
    :return: True if the device was flashed, False if flashing was skipped
    :raises FileNotFoundError: if the firmware images cannot be located
    :raises RuntimeError: if zero / multiple flasher ports are found, or esptool fails
    """
    if firmware_dir is None:
        root = search_root or os.path.dirname(os.path.abspath(__file__))
        firmware_dir = find_firmware_dir(root, AMBIT_FIRMWARE_FILES)
        if firmware_dir is None:
            raise FileNotFoundError(
                f"Could not find the Ambit firmware files "
                f"({', '.join(AMBIT_FIRMWARE_FILES)}) under {root!r}"
            )
    logger.info("Using firmware folder: %s", firmware_dir)

    if port is None:
        ports = flasher_ports()
        if not ports:
            raise RuntimeError("No Ambit flasher USB device found.")
        if len(ports) != 1:
            raise RuntimeError(
                f"Expected 1 flasher port, found {len(ports)}: {', '.join(ports)}"
            )
        port = ports[0]
    logger.info("Using flasher port: %s", port)

    if not force_flash:
        needs_flash, serial_log = detect_invalid_header(port)
        if not needs_flash:
            if serial_log.strip():
                logger.info("No 'invalid header' detected; flashing not required.")
            else:
                logger.warning("No serial output during probe; skipping flash.")
            return False

    flash_ambit(port, firmware_dir, layout=layout, chip=chip)
    return True


# ============================================================================
# MQTT publishing
# ============================================================================
# The same code is also available as the standalone `mqtt_publish` module
# (importable + runnable as a CLI). It's kept here too so `helpers.*` works
# on its own; `mqtt_publish` is preferred if it's importable.

def _resolve_cert_files(certs_dir):
    """Locate the AWS-IoT-style credential files inside ``certs_dir`` (searched recursively).

    Ignores macOS ``__MACOSX/`` directories and ``._*`` resource forks, so a
    folder straight out of a downloaded ``*_certs.zip`` works as-is.

    :return: (ca_file, cert_file, key_file)
    :raises FileNotFoundError: if any of the three cannot be found
    """
    try:
        from mqtt_publish import resolve_cert_files
        return resolve_cert_files(certs_dir)
    except ImportError:
        pass

    def _find(*patterns):
        for pat in patterns:
            hits = [h for h in glob.glob(os.path.join(certs_dir, "**", pat), recursive=True)
                    if "__MACOSX" not in h and not os.path.basename(h).startswith("._")]
            if hits:
                return sorted(hits)[0]
        raise FileNotFoundError(f"no file matching {patterns} under {certs_dir!r}")

    cert_file = _find("*-certificate.pem.crt", "*certificate*.pem*", "*.pem.crt", "*.crt")
    key_file  = _find("*-private.pem.key", "*private*.pem*", "*.pem.key", "*.key")
    ca_file   = _find("AmazonRootCA1.pem", "AmazonRootCA*.pem", "*RootCA*.pem", "*-CA*.pem", "*.pem")
    return ca_file, cert_file, key_file


def publish_payload_mqtt5(payload, topic, certs_dir, endpoint, *,
                          client_id=None, port=8883, qos=1, timeout=10.0):
    """Publish ``payload`` to ``topic`` over MQTT 5 with mutual-TLS auth (e.g. AWS IoT Core).

    :param payload: bytes / str sent verbatim; anything else (dict, list, ...) is json-encoded
    :param topic: MQTT topic to publish to
    :param certs_dir: folder holding the cert / key / CA files (see :func:`_resolve_cert_files`)
    :param endpoint: broker host; a ``scheme://host[:port][/path]`` URL is accepted too
    :param client_id: MQTT client id (default: the cert folder's basename)
    :param port: TLS port (default 8883; an explicit ``:port`` in ``endpoint`` wins)
    :param qos: publish QoS, 0 or 1
    :param timeout: seconds to wait for the connection and for the publish ack
    :return: True on success
    :raises ImportError: if paho-mqtt is not installed
    :raises ConnectionError / TimeoutError: on connect/publish failure
    """
    # Prefer the standalone module if it's importable; fall back to a local copy.
    try:
        from mqtt_publish import publish_mqtt5
        return publish_mqtt5(payload, topic, certs_dir, endpoint,
                             client_id=client_id, port=port, qos=qos, timeout=timeout)
    except ImportError:
        pass

    import ssl
    import threading
    try:
        import paho.mqtt.client as mqtt
        from paho.mqtt.enums import CallbackAPIVersion
    except ImportError as exc:  # pragma: no cover
        raise ImportError("publish_payload_mqtt5 needs paho-mqtt >= 2.0: pip install paho-mqtt") from exc

    ca_file, cert_file, key_file = _resolve_cert_files(certs_dir)
    if client_id is None:
        client_id = os.path.basename(os.path.normpath(certs_dir)) or "calibratron"

    # Accept a bare host, or a "scheme://host[:port][/path]" URL - reduce to the host.
    endpoint = endpoint.strip()
    if "://" in endpoint:
        endpoint = endpoint.split("://", 1)[1]
    endpoint = endpoint.split("/", 1)[0]
    if ":" in endpoint:
        host, _, maybe_port = endpoint.rpartition(":")
        if maybe_port.isdigit():
            endpoint, port = host, int(maybe_port)

    body = payload if isinstance(payload, (bytes, bytearray, str)) else json.dumps(payload)

    connected = threading.Event()
    conn_state = {}

    def _on_connect(client, userdata, flags, reason_code, properties=None):
        conn_state["rc"] = reason_code
        connected.set()

    client = mqtt.Client(callback_api_version=CallbackAPIVersion.VERSION2,
                         client_id=client_id, protocol=mqtt.MQTTv5)
    client.on_connect = _on_connect
    client.tls_set(ca_certs=ca_file, certfile=cert_file, keyfile=key_file,
                   tls_version=ssl.PROTOCOL_TLS_CLIENT)

    logger.info("MQTT5 connecting to %s:%d as %s ...", endpoint, port, client_id)
    client.connect(endpoint, port, keepalive=60)
    client.loop_start()
    try:
        if not connected.wait(timeout):
            raise TimeoutError(f"MQTT connect to {endpoint}:{port} timed out after {timeout}s")
        rc = conn_state.get("rc")
        if rc is not None and getattr(rc, "is_failure", False):
            raise ConnectionError(f"MQTT connect to {endpoint} rejected: {rc}")
        info = client.publish(topic, body, qos=qos)
        info.wait_for_publish(timeout)
        if not info.is_published():
            raise TimeoutError(f"publish to {topic!r} not acknowledged within {timeout}s")
    finally:
        client.loop_stop()
        client.disconnect()

    n = len(body if isinstance(body, (bytes, bytearray)) else body.encode())
    logger.info("MQTT5 published %d bytes to topic %r", n, topic)
    return True


# ============================================================================
# LED Control
# ============================================================================

def set_ambit_led(port, ledCurrent):
    """
    Turn the actinic LED on at the given current and leave it on.

    ``arrun`` latches the LED on; the device keeps it lit until it is reset or
    told otherwise. The port is opened without resetting the device (see
    :func:`_open_ambit_serial`), so the LED stays on after this call returns
    instead of being switched off by a reboot. Call with ``ledCurrent=0`` to
    switch the LED off.

    :param port: Serial port of the Ambit device
    :param ledCurrent: LED current value (integer); 0 turns the LED off
    """
    with _open_ambit_serial(port) as ser:
        ser.reset_input_buffer()
        _wait_for_device_ready(ser)
        ser.write(AmbitProto.LED_RUN.format(led=ledCurrent).encode())
        time.sleep(0.2)


# ============================================================================
# Data Analysis & Visualization
# ============================================================================

def r_squared(y_true, y_pred):
    """
    Calculate R² (coefficient of determination) for model fit quality.

    :param y_true: True values (array-like)
    :param y_pred: Predicted values (array-like)
    :return: R² value between 0 and 1
    """
    ss_res = np.sum((y_true - y_pred) ** 2)
    ss_tot = np.sum((y_true - np.mean(y_true)) ** 2)
    if ss_tot == 0:
        return 1.0 if ss_res == 0 else 0.0
    return 1 - ss_res / ss_tot


def plot_data_and_fit(x, y, coeffs, r2, output=None, xlabel="x", ylabel="y",
                      title="Data and Linear Fit", show=True):
    """
    Plot data points and linear fit with statistics.

    :param x: X values (array-like)
    :param y: Y values (array-like)
    :param coeffs: Polynomial coefficients from np.polyfit [slope, intercept]
    :param r2: R² value to display
    :param output: Optional file path to save the plot
    :param xlabel: Label for x-axis
    :param ylabel: Label for y-axis
    :param title: Figure title
    :param show: When False, the figure is only saved / kept in memory and
        ``plt.show()`` is not called. plt.show() blocks until the operator
        closes the window, which stalls an otherwise unattended calibration.
    """
    plt.figure(figsize=(8, 5))
    plt.scatter(x, y, color="blue", label="Data points")

    x_sort = np.linspace(np.min(x), np.max(x), 300)
    y_fit = np.polyval(coeffs, x_sort)
    plt.plot(x_sort, y_fit, color="red",
             label=f"lin fit: {coeffs[0]:.4g}x + {coeffs[1]:.4g}   R² = {r2:.8g}")

    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title(title)
    plt.grid(True)
    plt.legend()
    plt.tight_layout()

    if output:
        plt.savefig(output)
        print(f"Saved plot to {output}")

    if show:
        plt.show()
    else:
        plt.close()
