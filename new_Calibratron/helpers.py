"""Transport and device I/O for the new Calibratron.

Serial discovery, the Ambit binary + text consoles, the bench reference
instruments, and the calibration record. The tier math and the quality gates
live in :mod:`spec_cal`; the bench sequence lives in :mod:`run_calibratron`.

Deliberately dropped from the previous helpers.py, because the cmd-35 chain
replaces them rather than sitting alongside them:

  - ``get_par_AMB`` / cmd 31 / text ``get_par`` and ``PAR`` - the legacy PAR path.
    Integer ``Spec_COE`` weights on un-normalised counts at a pinned exposure,
    packed into a uint16 that wraps above ~11% of full scale.
  - ``set_par_gain`` / ``set_spec`` / ``spec_coef``. Still read and recorded
    (cmd 31 and deployed devices depend on it), never written - plan decision 7.
  - ``ambit_spec_unscale``, ``ambit_par_minipar_method``,
    ``AMBIT_PAR_DISCREPANCIES``. All three existed to characterise the mismatch
    between the legacy weighting and the MiniPAR's. Ambit now *ships* miniPar's
    fleet vector, so the mismatch they measured is gone.
  - ``basic_count_divisor`` in the seconds convention. Replaced by
    ``spec_cal.integration_time_ms``; the two differ by 1000x.
  - The arrun / ADPD trace recording per sweep point (diagnostic only, and it
    roughly doubled both sweeps). ``measure_adpd_baseline`` is kept - that one
    persists a calibration.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import serial
import serial.tools.list_ports

import spec_cal

# firmware_fetch lives at the repo root and is already release-policy tested, so
# it is imported rather than duplicated. APPENDED, never prepended: the repo root
# holds a *different* module also called `helpers`, and prepending would let it
# shadow this one on any later fresh import.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)


class _UnicodeSafeHandler(logging.StreamHandler):
    """StreamHandler that falls back to ASCII+backslashreplace when the stream's
    encoding (e.g. Windows cp1252) can't render a char. Without this a stray
    byte in serial output crashes the logger."""

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


logger = logging.getLogger("calibratron")
if not logger.handlers:
    _h = _UnicodeSafeHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


HERE = os.path.dirname(os.path.abspath(__file__))
BAUDRATE = 115200


def iso_timestamp():
    """UTC ISO 8601 with millisecond precision and a trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ============================================================================
# Discovery
# ============================================================================

_PORTS_CACHE = None
_PORT_INFO_CACHE = None

PORT_ROLE_CACHE_FILE = os.path.join(HERE, ".port_roles.json")


def invalidate_port_cache():
    """Clear the cached serial port list. Call after a USB topology change."""
    global _PORTS_CACHE, _PORT_INFO_CACHE
    _PORTS_CACHE = None
    _PORT_INFO_CACHE = None


def port_infos():
    """pyserial ``ListPortInfo`` for every port the OS reports, memoised."""
    global _PORT_INFO_CACHE
    if _PORT_INFO_CACHE is None:
        _PORT_INFO_CACHE = sorted(serial.tools.list_ports.comports(),
                                  key=lambda p: p.device)
    return list(_PORT_INFO_CACHE)


def serial_ports():
    """Available port names, from the OS enumeration rather than by probing."""
    global _PORTS_CACHE
    if _PORTS_CACHE is None:
        _PORTS_CACHE = [p.device for p in port_infos()]
    return list(_PORTS_CACHE)


def find_devices(specs, timeout=4.0, poll=0.25, ports=None, verbose=False):
    """Discover several serial devices in ONE pass over the available ports.

    Opens each port once, sends every still-unmatched question on each poll, and
    stops probing a port as soon as one role matches - a device only ever fills
    one role. Cost is roughly one ``timeout`` per unidentified port, whatever the
    number of roles.

    :param specs: ``{role: (question, answer)}``; ``answer`` is matched as a
        substring of everything the port has said so far
    :return: ``{role: port or None}`` for every role in ``specs``
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

                msg, matched = "", None
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
        except (OSError, serial.SerialException) as exc:
            logger.debug("Cannot open %s: %s", port, exc)
            invalidate_port_cache()
            continue

    for role in pending:
        logger.warning("No device found for role %r", role)
        found[role] = None
    return found


def load_port_roles(path=PORT_ROLE_CACHE_FILE):
    """Read the role -> port hints written by :func:`save_port_roles`."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items() if v}
    except (OSError, ValueError):
        return {}


def save_port_roles(roles, path=PORT_ROLE_CACHE_FILE):
    """Persist role -> port hints, merged over whatever is already stored."""
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

    The bench peripherals stay plugged in while Ambits are swapped, so on the
    second device onwards this is three quick confirmations plus a scan for the
    one port that moved.
    """
    found, remaining = {}, dict(specs)

    if use_cache:
        hints = load_port_roles(cache_path)
        present = set(serial_ports())
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
        claimed = set(found.values())
        rest = [p for p in serial_ports() if p not in claimed]
        found.update(find_devices(remaining, timeout=timeout, ports=rest, verbose=verbose))

    save_port_roles(found, cache_path)
    return found


# ============================================================================
# Protocols
# ============================================================================

class AmbitProto:
    """Wire protocol for the Ambit device.

    Text verbs for the five spectral/PAR vectors come from plan section 6.6: the
    console is what the Calibratron speaks, it reports accept/reject explicitly,
    and binary cmds 17/18 write *nothing* on an unrecognised subtype - costing an
    old image a full read timeout, and for cmd 18 desyncing the next header scan
    because the payload is never consumed.

    Only ``SET_PAR_SLOPE`` and ``SET_PAR_ICEPT`` are ever sent by this script.
    ``set_spec_offset`` / ``set_spec_sens`` / ``set_par_weight`` are listed for
    completeness and are deliberately absent: those three ship as firmware
    defaults (plan section 7) and a per-device tier-2 fit scores R^2 -31..-7443.
    """

    HELLO      = "hello\r\n"
    HELLO_ACK  = b"NEW"
    REBOOT     = "reboot\n"
    SET_NAME   = "set_name,{name}\n"

    # tier 3 - the Calibratron's entire calibration output for the PAR chain.
    # The device answers each setter with exactly one of three lines
    # (src/do_command.h report_spec_save):
    #     "<what> saved and verified"          -> committed to NVS and read back
    #     "<what> rejected"                    -> failed the predicate, NVS untouched
    #     "<what> save failed: <ESP_ERR_...>"  -> predicate passed, NVS write failed
    # The third is the one that matters: it contains neither "rejected" nor any
    # other negative keyword, so a host testing only for rejection reads an NVS
    # failure as a success. Acceptance is therefore tested POSITIVELY.
    SET_PAR_SLOPE = "set_par_slope,{value:.6f}\n"
    SET_PAR_ICEPT = "set_par_icept,{value:.6f}\n"
    GET_SPEC_CAL  = "get_spec_cal\n"

    #: The only reply that means the value reached NVS.
    SAVE_CONFIRMED = "saved and verified"

    # actinic LED (unrelated to the PAR chain, still a live calibration)
    SET_ACT      = "set_act, {coeff:.4f}\n"
    SET_CURRENTS = "set_currents,{i620:d},{i720:d},{ir:d},\n"
    LED_RUN      = "arrun1,1,1,2,0,0,1,0,1,{led:d},1,\n, \n"

    # ADPD photodiode trace. Type 2 (no IR reflect), far-red off, sample count
    # and frequency as hi,lo byte pairs, actinic setting, then the trailing 1 that
    # sets ambient sub-sampling to every point - which is the only reason `sun`
    # and `leaf` get populated at all.
    ARRUN2       = "arrun2,1,0,2,0,{nh:d},{nl:d},{fh:d},{fl:d},{act:d},1,\n, \n"

    # ADPD dark baseline
    MEASURE_BASELINE = "baseline,0\n"
    SET_BASELINE     = "set_baseline,{values}\n"


class MiniParProto:
    """Wire protocol for the MiniPAR. Plain text, one bare line per reply."""

    GET_PAR_CAL   = "par\n"             # par_raw * slope + intercept
    GET_PAR_RAW   = "par_raw\n"         # before slope/intercept
    GET_SPEC_RAW  = "spec_raw\n"        # "<model>,<raw counts...>", ends clear,nir
    SPEC_STATUS   = "spec_status\n"
    GET_SPEC_COEF = "get_spec_coeff\n"
    GET_CAL_PAR   = "get_cal_par\n"
    GET_NAME      = "get_name\n"


class DCSourceProto:
    """Wire protocol for the Kiprim DC source."""

    SET_VOLTAGE = "voltage {v:.3f}\r\n"
    SET_CURRENT = "current {i:.3f}\r\n"
    IDN         = "*IDN?\n"


#: Handshakes that identify each instrument on the bus.
DEVICE_SPECS = {
    "ambit":    (AmbitProto.HELLO,        "NEW"),
    "par_ref":  (MiniParProto.GET_NAME,   "Par_REF"),
    "emit_led": (MiniParProto.GET_NAME,   "Emit_LED"),
    "dc":       (DCSourceProto.IDN,       "KIPRIM"),
}

#: Ambit firmware >= 0.1.0 answers ``hello`` with "NEW <name> Ready FW:<version>".
HELLO_FW_RE = re.compile(r"FW:\s*([0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?)")


# ============================================================================
# Low-level serial
# ============================================================================

def _query(port, cmd, decode="utf-8", timeout=2.0):
    """Open, flush, write, readline. For the bench instruments (not the Ambit)."""
    with serial.Serial(port, baudrate=BAUDRATE, timeout=timeout) as ser:
        ser.flush()
        ser.write(cmd.encode())
        return ser.readline().decode(encoding=decode, errors="replace").strip()


def _command(port, cmd):
    """Open, flush, write. Fire-and-forget."""
    with serial.Serial(port, baudrate=BAUDRATE) as ser:
        ser.flush()
        ser.write(cmd.encode())


def open_ambit_serial(port, timeout=2.0):
    """Open the Ambit port WITHOUT triggering the device's auto-reset.

    The flasher bridge wires DTR/RTS to the ESP32-C3 reset/boot pins, so the
    usual "open then assert DTR/RTS" sequence reboots the device. Setting DTR/RTS
    to a steady state *before* opening avoids the reset edge, which matters both
    for a latched actinic LED and for keeping one port open across a whole sweep.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = BAUDRATE
    ser.timeout = timeout
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def _wait_for_ready(ser, expected=AmbitProto.HELLO_ACK, max_retries=10):
    """Poll ``hello`` until the device acknowledges. Returns the last reply."""
    resp = b""
    for _ in range(max_retries):
        ser.write(AmbitProto.HELLO.encode())
        resp = ser.readline()
        if expected in resp:
            return resp
        time.sleep(0.1)
    return resp


# ============================================================================
# Ambit link: one open port, text and binary on the same UART
# ============================================================================

class AmbitLink:
    """A held-open, non-resetting connection to one Ambit.

    Holding the port for a whole sweep matters for more than speed: every
    open-with-reset costs a boot cycle, and the sweep interleaves binary reads
    with text writes, which must land on the same session to be attributable to
    the same device state.

    Usage::

        with AmbitLink(port) as link:
            cal = link.spec_cal()
            reading = link.spec_raw()
    """

    def __init__(self, port, timeout=2.0):
        self.port = port
        self.timeout = timeout
        self._ser = None

    def __enter__(self):
        self._ser = open_ambit_serial(self.port, timeout=self.timeout)
        self._ser.reset_input_buffer()
        _wait_for_ready(self._ser)
        return self

    def __exit__(self, *exc):
        if self._ser is not None:
            self._ser.close()
            self._ser = None
        return False

    # ---- text -------------------------------------------------------------
    def text(self, cmd, n_lines=1, timeout=None):
        """Send a text command and read ``n_lines`` replies."""
        ser = self._require()
        old = ser.timeout
        if timeout is not None:
            ser.timeout = timeout
        try:
            ser.reset_input_buffer()
            ser.write(cmd.encode())
            lines = [ser.readline().decode("unicode_escape", errors="replace").strip()
                     for _ in range(n_lines)]
        finally:
            ser.timeout = old
        return lines[0] if n_lines == 1 else lines

    # ---- binary -----------------------------------------------------------
    def binary(self, frame, resp_size, timeout=5.0):
        """Send a binary frame and return the payload between 0xA1 and 0xF0.

        Scans for the 0xA1 start byte rather than assuming it is first: the
        console may have queued text, and the device's own log lines are ASCII
        (< 0x80) so they can never be mistaken for the marker.
        """
        ser = self._require()
        old = ser.timeout
        ser.timeout = timeout
        try:
            ser.reset_input_buffer()
            ser.write(frame)
            ser.flush()

            deadline = time.time() + timeout
            preamble = bytearray()
            while time.time() < deadline:
                byte = ser.read(1)
                if not byte:
                    continue
                if byte[0] == spec_cal.RESP_START:
                    break
                preamble += byte
            else:
                raise TimeoutError(
                    f"no 0xA1 response marker within {timeout}s "
                    f"(saw {bytes(preamble[-80:])!r})")

            payload = ser.read(resp_size)
            if len(payload) != resp_size:
                raise IOError(f"short binary payload: {len(payload)} of {resp_size} bytes")
            end = ser.read(1)
            if not end or end[0] != spec_cal.RESP_END:
                raise IOError(f"missing 0xF0 terminator, got {end!r}")
            return bytes(payload)
        finally:
            ser.timeout = old

    def _require(self):
        if self._ser is None:
            raise RuntimeError("AmbitLink used outside its with-block")
        return self._ser

    # ---- the two commands this script needs -------------------------------
    def spec_raw(self, timeout=5.0):
        """One cmd-35 reading.

        Timeout is generous by default: the measurement is two AS7341
        integrations at ATIME 99 / ASTEP 499, i.e. ~278 ms of the ~284 ms total
        (plan section 12), and the SMUX cannot present all 10 channels in one.
        """
        payload = self.binary(spec_cal.build_frame(spec_cal.CMD_SPEC_RAW),
                              spec_cal.SPEC_RAW_SIZE, timeout=timeout)
        return spec_cal.decode_spec_raw(payload)

    def spec_cal(self, timeout=3.0, allow_text_fallback=True):
        """The five calibration vectors, via cmd 33/4 with a text fallback.

        Binary first because it is exact; the ``get_spec_cal`` text mirror exists
        so a write can be confirmed without speaking binary (plan section 6.4)
        and is used only if the binary subtype is missing.
        """
        frame = spec_cal.build_frame(spec_cal.CMD_INFO, spec_cal.INFO_SUB_SPEC_CAL)
        try:
            payload = self.binary(frame, spec_cal.SPEC_CAL_SIZE, timeout=timeout)
            return spec_cal.decode_spec_cal(payload)
        except (TimeoutError, IOError, ValueError) as exc:
            if not allow_text_fallback:
                raise
            logger.warning("cmd 33/4 unavailable (%s) - falling back to get_spec_cal text", exc)
            # Five labelled lines: spec_offset, spec_sens, par_weight,
            # par_slope, par_intercept. A few extra are read so a stray log
            # line cannot truncate the set; the parser keys on labels.
            return spec_cal.parse_spec_cal_text(
                self.text(AmbitProto.GET_SPEC_CAL, n_lines=8, timeout=timeout))

    # ---- ADPD photodiode trace ------------------------------------------
    def set_actinic(self, setting):
        """Latch the actinic LED on at ``setting`` and leave it lit.

        On the link rather than its own connection so a sweep point can latch the
        LED, read the references and record an ADPD trace without ever closing
        the port - closing and reopening with DTR/RTS asserted would reset the
        device and drop the latch. Firmware forces the LED off for settings <= 3.
        """
        self.text(AmbitProto.LED_RUN.format(led=int(setting)), timeout=2.0)
        time.sleep(0.3)
        self._require().reset_input_buffer()

    def zero_pulse_currents(self):
        """Zero the ADPD pulse LEDs (620 / 720 / IR).

        Without this the detector sees its own pulse LEDs and ``sun`` / ``leaf``
        stop being a measurement of the incident light, which is the whole point
        of recording them during a sweep. Re-asserted before every trace rather
        than once per session: it is one write, and if anything did reset the
        device mid-sweep the currents would silently come back non-zero and every
        later photodiode reading would be contaminated with no sign of it.

        :return: True if the device echoed the expected confirmation
        """
        echo = self.text(AmbitProto.SET_CURRENTS.format(i620=0, i720=0, ir=0),
                         timeout=2.0)
        if "Currents set" not in echo:
            logger.warning("unexpected set_currents echo: %r", echo)
            return False
        return True

    def arrun(self, actinic=0, num_points=5, freq=10, timeout=15.0):
        """Record an ADPD trace and return the parsed per-channel buffers.

        The device answers with one ``Data:<tag>,Length:N<TAB><v>,<v>,...`` line
        per channel buffer, then ``Data sent``.

        :param actinic: actinic setting driven during the run (0 = off)
        :return: ``{"actinic", "num_points", "freq_hz", "pulse_currents_zeroed",
            "data": {tag: [ints]}}``, or None if nothing arrived
        """
        zeroed = self.zero_pulse_currents()

        nh, nl = divmod(int(num_points), 256)
        fh, fl = divmod(int(freq), 256)
        ser = self._require()
        old_timeout = ser.timeout
        ser.timeout = 2.0
        data, got_end = {}, False
        try:
            ser.reset_input_buffer()
            ser.write(AmbitProto.ARRUN2.format(nh=nh, nl=nl, fh=fh, fl=fl,
                                               act=int(actinic)).encode())
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if line.startswith("Data:"):
                    head, _, values = line.partition(chr(9))
                    tag = head[len("Data:"):].split(",", 1)[0]
                    try:
                        data[tag] = [int(v) for v in values.split(",") if v.strip()]
                    except ValueError:
                        data[tag] = values        # keep an unparseable payload
                elif "Data sent" in line:
                    got_end = True
                    break
        finally:
            ser.timeout = old_timeout

        if not got_end:
            logger.warning("arrun: 'Data sent' not received within %ss (tags: %s)",
                           timeout, sorted(data))
        if not data:
            return None
        return {"actinic": int(actinic), "num_points": int(num_points),
                "freq_hz": int(freq), "pulse_currents_zeroed": zeroed,
                "truncated": not got_end, "data": data}

    # ---- tier-3 setters ---------------------------------------------------
    def set_par_slope(self, value):
        """Write ``par_slope``. Returns the value actually put on the wire."""
        return self._set_tier3(AmbitProto.SET_PAR_SLOPE, value, "par_slope",
                               spec_cal.valid_par_slope)

    def set_par_intercept(self, value):
        """Write ``par_intercept``. Returns the value actually put on the wire."""
        return self._set_tier3(AmbitProto.SET_PAR_ICEPT, value, "par_intercept",
                               spec_cal.valid_par_intercept)

    def _set_tier3(self, template, value, label, predicate):
        # Round first, then validate what will actually be transmitted: the wire
        # format is fixed-point, so a value that passes the predicate at full
        # precision can fail it after rounding (e.g. a slope of 4e-7 -> 0.000000,
        # which the firmware rejects as outside (0, 100]).
        wire = round(float(value), 6)
        if not predicate(wire):
            raise ValueError(f"{label} {wire!r} fails the firmware predicate; refusing to send")
        reply = self.text(template.format(value=wire), n_lines=2, timeout=3.0)
        confirmed = any(AmbitProto.SAVE_CONFIRMED in line for line in reply)
        if not confirmed:
            # One positive test covers "rejected", "save failed: ESP_ERR_..."
            # and any future wording, because only the confirmation passes.
            raise RuntimeError(f"device did not confirm {label}={wire}: {reply!r}")
        time.sleep(0.2)          # let the NVS commit settle
        return wire


def get_spec_raw(port, timeout=5.0):
    """One-shot cmd-35 read on its own connection."""
    with AmbitLink(port) as link:
        return link.spec_raw(timeout=timeout)


def get_spec_cal(port, timeout=3.0):
    """One-shot calibration read-back on its own connection."""
    with AmbitLink(port) as link:
        return link.spec_cal(timeout=timeout)


def probe_cmd35(port):
    """Is this firmware's cmd 35 the 80-byte three-tier form?

    Probes the opcode rather than gating on a version number. Version gating is
    broken by construction here: ``tools/version.py`` keeps the leading X.Y.Z of
    ``git describe``, so every dev build reports the tag it descends from - a
    build of the rewrite branch still says 1.1.4 (plan section 9).

    :return: ``(ok, detail)``
    """
    try:
        with AmbitLink(port) as link:
            reading = link.spec_raw()
    except ValueError as exc:                # wrong length or unknown format
        return False, str(exc)
    except (TimeoutError, IOError) as exc:
        return False, f"cmd 35 did not answer: {exc}"
    return True, (f"cmd 35 format {reading.format}, {spec_cal.SPEC_RAW_SIZE} B, "
                  f"tint {reading.tint_ms:.0f} ms, gains "
                  f"{spec_cal.gain_multiplier(reading.gain_low):g}x/"
                  f"{spec_cal.gain_multiplier(reading.gain_high):g}x")


def write_tier3_with_readback(port, *, par_slope, par_intercept, previous):
    """Write both tier-3 parameters, verify by read-back, roll back on mismatch.

    Read-back cannot go through the reboot dump: the five vectors live outside
    ``ambit_calibration_info_t`` on purpose (plan decision 2), so nothing about
    them appears in the boot banner. It has to be cmd 33/4 (or its text mirror).

    Both parameters are written before either is verified. They are two separate
    NVS commits, so a failure between them leaves ``a`` new and ``b`` old - a
    bounded, self-consistent state, unlike a torn blob, and one this function
    then rolls back.

    :param previous: ``(par_slope, par_intercept)`` read before the write, needed
        because the device carries no record of its own prior value
    :return: the verified :class:`spec_cal.SpecCal`
    :raises RuntimeError: if verification fails; the rollback outcome is included
    """
    prev_slope, prev_intercept = float(previous[0]), float(previous[1])

    with AmbitLink(port) as link:
        wire_slope = link.set_par_slope(par_slope)
        wire_intercept = link.set_par_intercept(par_intercept)
        observed = link.spec_cal()

        ok = (abs(observed.par_slope - wire_slope) <= 1e-4 * max(1.0, abs(wire_slope))
              and abs(observed.par_intercept - wire_intercept) <= 1e-4 * max(1.0, abs(wire_intercept)))
        if ok:
            logger.info("tier 3 verified: par_slope %.6g -> %.6g, par_intercept %.6g -> %.6g",
                        prev_slope, observed.par_slope, prev_intercept, observed.par_intercept)
            return observed

        restore_error = None
        try:
            link.set_par_slope(prev_slope)
            link.set_par_intercept(prev_intercept)
            restored = link.spec_cal()
            if (abs(restored.par_slope - prev_slope) > 1e-4 * max(1.0, abs(prev_slope))
                    or abs(restored.par_intercept - prev_intercept) > 1e-4 * max(1.0, abs(prev_intercept))):
                restore_error = (f"restore read back ({restored.par_slope:.6g}, "
                                 f"{restored.par_intercept:.6g})")
        except Exception as exc:              # keep the original failure context
            restore_error = str(exc)

    detail = (f"; previous tier 3 restoration failed: {restore_error}" if restore_error
              else "; previous tier 3 restored")
    raise RuntimeError(
        f"tier-3 write was not verified (read ({observed.par_slope:.6g}, "
        f"{observed.par_intercept:.6g}), expected ({wire_slope:.6g}, "
        f"{wire_intercept:.6g})){detail}")


# ============================================================================
# Bench references
# ============================================================================

def set_current(port, current):
    """Set the DC source output current, in amps."""
    _command(port, DCSourceProto.SET_CURRENT.format(i=current))


def set_voltage(port, voltage):
    """Set the DC source output voltage, in volts."""
    _command(port, DCSourceProto.SET_VOLTAGE.format(v=voltage))


def get_par_MP(port):
    """The MiniPAR's calibrated PAR, in umol m-2 s-1."""
    return float(_query(port, MiniParProto.GET_PAR_CAL))


def get_par_raw_MP(port):
    """The MiniPAR's PAR before its own slope/intercept, or None."""
    resp = _query(port, MiniParProto.GET_PAR_RAW)
    try:
        return float(resp)
    except ValueError:
        logger.warning("MiniPAR raw PAR unavailable (reply: %r)", resp)
        return None


def get_spec_raw_MP(port):
    """The MiniPAR's raw channel counts.

    Order is F1..F8, **CLEAR, NIR** - the last two are the opposite way round
    from ambit. :func:`minipar_to_ambit_order` does the swap; plan section 8
    explains why getting it wrong fails quietly rather than loudly.
    """
    resp = _query(port, MiniParProto.GET_SPEC_RAW)
    try:
        model, *counts = resp.split(",")
        if model.startswith("error") or not counts:
            raise ValueError(resp)
        return {"model": model, "counts": [int(c) for c in counts]}
    except ValueError:
        logger.warning("MiniPAR raw spectrum unavailable (reply: %r)", resp)
        return None


def minipar_to_ambit_order(values):
    """Reorder a miniPar-ordered 10-vector into ambit's order (swap the last two).

    NIR and Clear carry the most dissimilar coefficients in every vector
    (``spec_sens`` 5.78 vs 31.57, ``par_weight`` -37.7 vs +16.9), so a missed
    swap produces a confidently wrong number rather than an obvious one.
    """
    if not values or len(values) != spec_cal.N_CHANNELS:
        return None
    by_name = dict(zip(spec_cal.MINIPAR_CHANNELS, values))
    return [by_name[name] for name in spec_cal.CHANNELS]


def get_spec_status_MP(port):
    """The MiniPAR's live acquisition settings, or None."""
    resp = _query(port, MiniParProto.SPEC_STATUS)
    kv = dict(tok.split("=", 1) for tok in resp.split(",") if "=" in tok)
    if "model" not in kv:
        logger.warning("MiniPAR status unavailable (reply: %r)", resp)
        return None
    out = {"model": kv["model"]}
    for key in ("available", "atime", "astep", "gain"):
        try:
            out[key] = int(kv[key])
        except (KeyError, ValueError):
            out[key] = None
    return out


def get_spec_coeff_MP(port):
    """The MiniPAR's per-channel PAR coefficients, read off the device."""
    resp = _query(port, MiniParProto.GET_SPEC_COEF)
    try:
        return [float(v) for v in resp.split(",")]
    except ValueError:
        logger.warning("MiniPAR coefficients unavailable (reply: %r)", resp)
        return None


def get_cal_par_MP(port):
    """The MiniPAR's own PAR slope / intercept, or None."""
    resp = _query(port, MiniParProto.GET_CAL_PAR)
    kv = dict(tok.split("=", 1) for tok in resp.split(",") if "=" in tok)
    try:
        return {"slope": float(kv["slope"]), "intercept": float(kv["intercept"])}
    except (KeyError, ValueError):
        logger.warning("MiniPAR calibration unavailable (reply: %r)", resp)
        return None


def read_minipar_reference(port):
    """Snapshot the reference MiniPAR's identity and settings.

    This defines the PAR that ambit's tier 3 is anchored to, so it is recorded
    in full - see :data:`spec_cal.REFERENCE_TRANSFER_NOTE` for what accepting a
    MiniPAR instead of a Li-250A costs, and why one later Li-250A comparison can
    retire it without re-running the bench.
    """
    return {
        "name": _query(port, MiniParProto.GET_NAME),
        "spec_status": get_spec_status_MP(port),
        "par_coefficients": get_spec_coeff_MP(port),
        "calibration": get_cal_par_MP(port),
        "transfer_note": dict(spec_cal.REFERENCE_TRANSFER_NOTE),
    }


# ============================================================================
# Ambit boot dump
# ============================================================================

@dataclass
class AmbitInfo:
    """Ambit device information parsed from a reboot dump.

    This is ``ambit_calibration_info_t`` as the console prints it. Note what is
    NOT here: none of the five spectral/PAR vectors, by design (plan decision 2).
    ``light_slope`` is the legacy ``spec_coef`` - recorded because cmd 31 and
    deployed devices still use it, never written by this script.
    """

    FW: bytes = b""
    IsValid: bool = False
    name: bytes = b""
    MAC: str = ""
    fw_size: int = 0
    fw_date: str = ""
    adpd_chip_version: "int | None" = None
    metadata: dict = field(default_factory=dict)
    act_led_coeff: float = 0.0
    light_slope: float = 0.0            # legacy spec_coef; read-only here
    emit_coeff: float = 0.0
    sun_coeff: float = 0.0
    temp_offset: float = 0.0
    temp_slope: float = 0.0
    actinic_curve: dict = field(default_factory=dict)
    adpd_calibration: list = field(default_factory=list)
    mlx_calibration: list = field(default_factory=list)

    def process_line(self, line):
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
            self.metadata = {k: _coerce_num(v)
                             for k, v in _kv_pairs(text[len("Metadata:"):]).items()}
            return

        if text.startswith("Calibration:"):
            payload = text[len("Calibration:"):].strip()
            if payload.startswith("ADPD"):
                _, vals = payload.split(":", 1)
                self.adpd_calibration = [_coerce_num(v) for v in vals.split()]
                return
            kv = _kv_pairs(payload)
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
            self.mlx_calibration = [_coerce_num(v) for v in text[len("MLX:"):].split() if v]
            return

        if text.startswith("FW:"):
            body = text[len("FW:"):].strip()
            if "MAC:" in body:
                # Tab-separated; the Date value contains spaces ("Mar  5 2026").
                kv = _kv_pairs(body, sep="\t")
                self.MAC = kv.get("MAC", "")
                self.fw_size = int(kv["Size"]) if kv.get("Size", "").isdigit() else 0
                self.fw_date = kv.get("Date", "")
            else:
                self.FW = body.encode()
                self.IsValid = True

    @property
    def firmware(self):
        return self.FW.decode(errors="replace").strip()

    @property
    def device_name(self):
        return self.name.decode(errors="replace").strip()

    def to_dict(self):
        return {
            "FW": self.firmware,
            "IsValid": self.IsValid,
            "name": self.device_name,
            "MAC": self.MAC,
            "fw_size": self.fw_size,
            "fw_date": self.fw_date,
            "adpd_chip_version": self.adpd_chip_version,
            "act_led_coeff": self.act_led_coeff,
            "light_slope_legacy_spec_coef": self.light_slope,
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
        return (f"FW: {self.firmware} (MAC={self.MAC}, size={self.fw_size}B, date={self.fw_date})\n"
                f"Name: {self.device_name}, valid: {self.IsValid}\n"
                f"Legacy spec_coef (cmd 31 only): {self.light_slope}\n"
                f"Actinic: {self.act_led_coeff}, Emit: {self.emit_coeff}, Sun: {self.sun_coeff}\n"
                f"Actinic curve: {self.actinic_curve}\n"
                f"ADPD cal: {self.adpd_calibration} (chip v{self.adpd_chip_version})")


def _kv_pairs(text, sep=None):
    """Parse 'k:v<sep>k:v ...'. Default splits on whitespace and commas."""
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
        return float(v) if "." in v else int(v)
    except (ValueError, TypeError):
        return v


def ambit_reboot(port, max_lines=26):
    """Reboot the Ambit and parse its boot dump into an :class:`AmbitInfo`."""
    info = AmbitInfo()

    with serial.Serial(port, baudrate=BAUDRATE, timeout=2.0) as ser:
        ser.flush()
        resp = _wait_for_ready(ser)
        if AmbitProto.HELLO_ACK not in resp:
            logger.warning("Ambit on %s never acknowledged hello before reboot", port)
        ser.write(AmbitProto.REBOOT.encode())
        for _ in range(max_lines):
            line = ser.readline()
            info.process_line(line)
            logger.debug("boot line: %s", line)
            if b"FW:" in line and b"MAC" not in line:
                info.IsValid = True
                break

    with serial.Serial(port, baudrate=BAUDRATE, timeout=2.0) as ser:   # back online?
        ser.flush()
        _wait_for_ready(ser)
    return info


def detect_ambit_version(port):
    """Firmware version from the ``hello`` reply, falling back to the boot dump."""
    try:
        with AmbitLink(port) as link:
            reply = link.text(AmbitProto.HELLO, timeout=2.0)
    except Exception as exc:
        logger.warning("could not read the hello reply on %s: %s", port, exc)
        reply = ""
    match = HELLO_FW_RE.search(reply or "")
    if match:
        return match.group(1).strip()
    return ambit_reboot(port).firmware or None


def set_ambit_name(port, name):
    """Set the Ambit device name."""
    with AmbitLink(port) as link:
        link.text(AmbitProto.SET_NAME.format(name=name), timeout=2.0)
        time.sleep(0.2)


# ============================================================================
# Actinic LED and ADPD dark baseline (unrelated to the PAR chain, still live)
# ============================================================================

def set_ambit_led(port, current):
    """Latch the actinic LED on its own connection. See :meth:`AmbitLink.set_actinic`."""
    with AmbitLink(port) as link:
        link.set_actinic(current)


def record_arrun(port, actinic=0, num_points=5, freq=10, timeout=15.0):
    """One ADPD trace on its own connection. See :meth:`AmbitLink.arrun`."""
    with AmbitLink(port) as link:
        return link.arrun(actinic=actinic, num_points=num_points, freq=freq,
                          timeout=timeout)


def set_ambit_led_gain(port, coeff):
    """Persist the actinic LED gain (``act_led_coeff``)."""
    with AmbitLink(port) as link:
        reply = link.text(AmbitProto.SET_ACT.format(coeff=float(coeff)), timeout=3.0)
        if "reject" in reply.lower() or "failed" in reply.lower():
            raise RuntimeError(f"device did not accept act_led_coeff={coeff}: {reply!r}")
        time.sleep(0.2)


def measure_adpd_baseline(port, timeout=20.0):
    """Measure, but do not persist, the six-channel ADPD dark baseline."""
    with serial.Serial(port, baudrate=BAUDRATE, timeout=timeout) as ser:
        ser.flush()
        _wait_for_ready(ser)
        ser.write(AmbitProto.MEASURE_BASELINE.encode())
        for _ in range(12):
            parts = ser.readline().decode("utf-8", errors="replace").strip().split(",")
            if len(parts) != 6:
                continue
            try:
                values = [int(v) for v in parts]
            except ValueError:
                continue
            if all(0 <= v <= 0xFFFFFF for v in values):
                return values
    raise RuntimeError("Ambit did not return a valid six-channel ADPD baseline")


def set_adpd_baseline(port, values, timeout=5.0):
    """Persist one complete baseline vector and require firmware verification."""
    if len(values) != 6 or any(isinstance(v, bool) or not isinstance(v, int)
                               or v < 0 or v > 0xFFFFFF for v in values):
        raise ValueError("ADPD baseline must contain six unsigned 24-bit integers")
    command = AmbitProto.SET_BASELINE.format(values=",".join(str(v) for v in values))
    with serial.Serial(port, baudrate=BAUDRATE, timeout=timeout) as ser:
        ser.flush()
        _wait_for_ready(ser)
        ser.write(command.encode())
        response = ser.readline().decode("utf-8", errors="replace").strip()
    if response != "Baseline saved and verified":
        raise RuntimeError(f"Ambit baseline write was not verified: {response!r}")


# ============================================================================
# Firmware
# ============================================================================

FLASHER_VID, FLASHER_PID = 0x1A86, 0x55D3      # WCH CH343 bridge
FLASHER_VIDPID = "1A86:55D3"


def flasher_ports():
    """Serial ports that look like an Ambit flasher (WCH CH343 bridge)."""
    found = []
    for port in sorted(serial.tools.list_ports.comports(), key=lambda p: p.device):
        device = getattr(port, "device", None)
        if not device:
            continue
        hwid = (getattr(port, "hwid", "") or "").upper()
        if ((getattr(port, "vid", None), getattr(port, "pid", None))
                == (FLASHER_VID, FLASHER_PID)) or FLASHER_VIDPID in hwid:
            found.append(device)
    return found


def esptool_command():
    """The argv prefix used to invoke esptool, cross-platform.

    Prefers an ``esptool.exe`` bundled in the repo on Windows; otherwise runs the
    installed package via ``python -m esptool``.
    """
    import importlib.util

    if os.name == "nt":
        for candidate in glob.glob(os.path.join(_REPO_ROOT, "**", "esptool.exe"),
                                   recursive=True):
            return [candidate]
    if importlib.util.find_spec("esptool") is not None:
        return [sys.executable, "-m", "esptool"]
    raise RuntimeError("esptool not found - `pip install esptool`, or place "
                       "esptool.exe in the Calibratron folder (Windows only)")


def read_flash_layout(firmware_dir, manifest_name="manifest.json"):
    """Read the esptool flash layout out of a firmware folder's manifest.

    The manifest's ``flash`` array is the contract with the firmware repo.

    :return: ``[(offset, filename), ...]`` ascending by offset
    """
    manifest_path = os.path.join(firmware_dir, manifest_name)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"no {manifest_name} in {firmware_dir} - the folder must be one "
            f"produced by firmware_fetch.fetch_latest()") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{manifest_path} is not valid JSON: {exc}") from exc

    entries = manifest.get("flash") if isinstance(manifest, dict) else None
    if not entries:
        raise RuntimeError(f"{manifest_path} lists no 'flash' entries")

    layout = []
    for entry in entries:
        name = entry.get("file") if isinstance(entry, dict) else None
        offset = entry.get("offset") if isinstance(entry, dict) else None
        if not name or offset is None:
            raise RuntimeError(f"{manifest_path}: 'flash' entry missing file/offset: {entry!r}")
        if not os.path.isfile(os.path.join(firmware_dir, str(name))):
            raise FileNotFoundError(f"{manifest_path} lists {name!r} but it is missing")
        layout.append((str(offset), str(name)))

    def _offset_value(item):
        try:
            return int(item[0], 0)
        except (TypeError, ValueError):
            return 0

    layout.sort(key=_offset_value)
    return layout


def flash_ambit_firmware(firmware_dir, port=None, chip="esp32c3"):
    """Flash the verified images in ``firmware_dir`` with esptool.

    Self-contained rather than delegating to the repo-root ``helpers``: that
    module is a different module of the same name, so importing it from here
    resolves back to this one. Release *selection* policy still lives in
    ``firmware_fetch``; only the esptool invocation is duplicated.

    esptool runs with ``cwd=firmware_dir`` and bare file names, which keeps the
    command line free of the spaces that Windows bench paths are full of.

    :return: True once the device has been flashed
    :raises RuntimeError: if the folder is not a verified release, if zero or
        several flasher ports are present, or if esptool exits non-zero
    """
    import subprocess

    import firmware_fetch
    if not firmware_fetch.is_complete(firmware_dir):
        raise RuntimeError(f"{firmware_dir} is not a complete verified Ambit "
                           f"release cache entry")
    layout = read_flash_layout(firmware_dir)

    if port is None:
        candidates = flasher_ports()
        if not candidates:
            raise RuntimeError("no Ambit flasher USB device found")
        if len(candidates) != 1:
            raise RuntimeError(f"expected 1 flasher port, found {len(candidates)}: "
                               f"{', '.join(candidates)}")
        port = candidates[0]
    logger.info("flashing via %s", port)

    # The snake_case esptool options are the deprecated spelling in esptool v5,
    # but v5 still accepts them, so one command line works with both the bundled
    # v4 esptool.exe and a pip-installed v5.
    cmd = [*esptool_command(), "--chip", chip, "--baud", "921600", "--port", port,
           "--before", "default_reset", "--after", "hard_reset",
           "write_flash", "-z", "--flash_mode", "keep",
           "--flash_freq", "keep", "--flash_size", "keep"]
    for offset, image in layout:
        cmd += [offset, image]

    logger.info("esptool layout: %s", ", ".join(f"{o}:{i}" for o, i in layout))
    result = subprocess.run(cmd, cwd=str(firmware_dir))
    if result.returncode != 0:
        raise RuntimeError(f"esptool exited with return code {result.returncode}")
    logger.info("flash completed")
    return True


# ============================================================================
# The calibration record
# ============================================================================

#: Boot-dump fields dropped from the record. The GPS/IMU block is placeholder
#: data on the bench (no fix indoors) and IsValid is a parsing artefact.
DROPPED_FIELDS = ("metadata", "IsValid")


def _device_dict(info):
    return {k: v for k, v in info.to_dict().items() if k not in DROPPED_FIELDS}


def make_calibration_payload(info_before, info_after, *,
                             spec_par_cal=None, led_cal=None, baseline_cal=None,
                             station=None, protocol_id="CALIBRATION",
                             device_id=None, device_name=None, indent=None):
    """Build the openJII calibration payload.

    ``sample`` is a JSON *string*, not a nested array: openJII's ``sensor_schema``
    declares it ``StringType`` and ``from_json`` yields null on a type mismatch,
    which silently drops the whole calibration on ingest. The topic, client id
    and ingestion timestamps are added by the AWS IoT rule, so none are sent.

    :raises ValueError: if either dump is missing or never populated
    """
    empty = [label for label, info in (("info_before", info_before),
                                       ("info_after", info_after))
             if info is None or not getattr(info, "IsValid", False)]
    if empty:
        raise ValueError(f"cannot build the calibration payload: {', '.join(empty)} "
                         f"empty / not populated - call ambit_reboot() first")

    device = _device_dict(info_after)
    before = _device_dict(info_before)
    changed = {k: v for k, v in before.items() if device.get(k) != v}

    sample = [{
        "protocol_id": protocol_id,
        "set": [{
            "device": device,
            "device_before": changed or None,
            "spec_par_calibration": spec_par_cal,
            "led_calibration": led_cal,
            "adpd_baseline_calibration": baseline_cal,
            "station": station,
        }],
    }]

    payload = {
        "sample": json.dumps(sample, separators=(",", ":")),
        "device_id": device_id or device.get("MAC") or "MACID",
        "device_name": device_name or device.get("name") or "NAME",
        "device_version": "1",
        "device_firmware": device.get("FW") or "1",
        "timestamp": iso_timestamp(),
    }
    return json.dumps(payload, indent=indent)


def save_payload(payload, mac=None, directory=None):
    """Write the payload to ``<directory>/<YYYY-MM-DD_HH-MM-SS>_<MAC>.json``.

    Stored indented and with ``sample`` expanded so a saved calibration stays
    readable; what goes on the wire stays the compact openJII form.
    """
    directory = directory or os.path.join(HERE, "calibrations")
    data = json.loads(payload) if isinstance(payload, str) else payload
    mac = mac or data.get("device_id") or "UNKNOWN"

    readable = dict(data)
    if isinstance(readable.get("sample"), str):
        try:
            readable["sample"] = json.loads(readable["sample"])
        except ValueError:
            pass

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{mac}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(readable, f, indent=2)
    logger.info("wrote %s", path)
    return path


def _resolve_cert_files(certs_dir):
    """Locate the AWS-IoT credential files under ``certs_dir`` (recursive).

    Ignores macOS ``__MACOSX/`` directories and ``._*`` resource forks, so a
    folder straight out of a downloaded ``*_certs.zip`` works as-is.
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
    ca_file   = _find("AmazonRootCA1.pem", "AmazonRootCA*.pem", "*RootCA*.pem", "*.pem")
    return ca_file, cert_file, key_file


def publish_payload_mqtt5(payload, topic, certs_dir, endpoint, *,
                          client_id=None, port=8883, qos=1, timeout=10.0):
    """Publish over MQTT 5 with mutual-TLS auth (AWS IoT Core)."""
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
    except ImportError as exc:
        raise ImportError("publish_payload_mqtt5 needs paho-mqtt >= 2.0") from exc

    ca_file, cert_file, key_file = _resolve_cert_files(certs_dir)
    if client_id is None:
        client_id = os.path.basename(os.path.normpath(certs_dir)) or "calibratron"

    endpoint = endpoint.strip()
    if "://" in endpoint:
        endpoint = endpoint.split("://", 1)[1]
    endpoint = endpoint.split("/", 1)[0]
    if ":" in endpoint:
        host, _, maybe_port = endpoint.rpartition(":")
        if maybe_port.isdigit():
            endpoint, port = host, int(maybe_port)

    body = payload if isinstance(payload, (bytes, bytearray, str)) else json.dumps(payload)
    connected, conn_state = threading.Event(), {}

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

    logger.info("MQTT5 published %d bytes to %r",
                len(body if isinstance(body, (bytes, bytearray)) else body.encode()), topic)
    return True
