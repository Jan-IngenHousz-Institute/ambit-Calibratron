"""
Helper functions for Ambit calibration and PAR measurements.

This module contains utilities for:
- Serial device communication and discovery
- PAR (Photosynthetically Active Radiation) measurements
- Data analysis and visualization
- Device calibration
"""

import os
import re
import sys
import time
import json
import logging
import serial
import serial.tools.list_ports
import subprocess
import importlib.util
import glob
from pathlib import Path
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


def _invalidate_port_cache():
    """Clear the cached serial port list. Call after USB topology changes."""
    global _PORTS_CACHE
    _PORTS_CACHE = None


def serial_ports():
    """
    Lists available serial port names for the current platform.

    Memoised after the first call. Call _invalidate_port_cache() if devices
    have been hot-plugged since the last scan.

    :raises EnvironmentError: On unsupported or unknown platforms
    :returns: A list of the serial ports available on the system
    """
    global _PORTS_CACHE
    if _PORTS_CACHE is not None:
        return list(_PORTS_CACHE)

    if sys.platform.startswith('win'):
        ports = ['COM%s' % (i + 1) for i in range(256)]
    elif sys.platform.startswith('linux') or sys.platform.startswith('cygwin'):
        ports = glob.glob('/dev/tty[A-Za-z]*')
    elif sys.platform.startswith('darwin'):
        ports = glob.glob('/dev/tty.*')
    else:
        raise EnvironmentError('Unsupported platform')

    result = []
    for port in ports:
        try:
            s = serial.Serial(port)
            s.close()
            result.append(port)
        except (OSError, serial.SerialException):
            pass
    _PORTS_CACHE = result
    return list(result)


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
    """Wire protocol for the MiniPAR device."""
    GET_PAR_RAW  = "par_raw\n"
    GET_PAR_CAL  = "par\n"
    GET_SPEC_RAW = "spec_raw\n"
    GET_NAME     = "get_name\n"
    SET_NAME     = "set_name,{name}\n"


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


def _ambit_query(port, cmd, decode="unicode_escape", timeout=2.0):
    """Open, flush, readiness handshake, write, readline. For Ambit reads."""
    with serial.Serial(port, baudrate=BAUDRATE, timeout=timeout) as ser:
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


def make_calibration_payload(info_precalibration=None, info_postcalibration=None, *,
                             device_id=None, device_name=None,
                             firmware_version=None, device_firmware=None,
                             device_version="1", protocol_id="CALIBRATION",
                             par_cal=None, led_cal=None,
                             indent=2):
    """Build the JSON calibration-upload payload from the pre/post AmbitInfo dumps.

    The ``device_*`` / ``firmware_version`` fields default to values read from
    ``info_postcalibration`` (falling back to ``info_precalibration``); pass
    explicit strings to override any of them.

    :param info_precalibration: AmbitInfo captured before calibration
    :param info_postcalibration: AmbitInfo captured after calibration
    :param par_cal: PAR-sensor calibration block (x/y arrays + labels + slope/r2)
        from calibrate_par_sensor(); ``None`` (-> JSON null) if it was skipped
    :param led_cal: actinic-LED calibration block, same shape, from
        calibrate_led(); ``None`` if it was skipped
    :param indent: json.dumps indent (None for a compact one-line payload)
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

    payload = {
        "sample": [
            {
                "protocol_id": protocol_id,
                "set": [
                    {
                        "METADATA_PRECALIBRATION":  info_precalibration.to_dict(),
                        "METADATA_POSTCALIBRATION": info_postcalibration.to_dict(),
                        "PAR_SENSOR_CALIBRATION":   par_cal,
                        "LED_CALIBRATION":          led_cal,
                    }
                ],
            }
        ],
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
# Self-contained Ambit firmware flasher: it resolves esptool, reads the flash
# layout from the firmware release manifest, finds the flasher serial port and
# runs the flash - so the calibration tooling can (re)flash an Ambit without
# shelling out to any external uploader script.
#
# The images are no longer vendored in this repo. ``firmware_fetch.py`` pulls
# the latest public AMBIT release into ``firmware_cache/<version>/`` and the folder
# it returns is what gets flashed. That folder always carries a ``manifest.json``
# describing which file goes at which offset, so a layout change on the firmware
# side (extra partition, renamed image, ...) needs no change here.

# WCH CH343 USB-serial bridge used by the Ambit flasher.
FLASHER_VID = 0x1A86
FLASHER_PID = 0x55D4
FLASHER_VIDPID = "1A86:55D4"

# Release manifest that describes the flash layout; written by the public AMBIT
# release pipeline and downloaded alongside the images.
AMBIT_MANIFEST_NAME = "manifest.json"

# Folder this module lives in. The bundled Windows esptool.exe is looked up
# here (recursively) - it is part of the repo, not of the firmware download.
REPO_DIR = os.path.dirname(os.path.abspath(__file__))

# Default cache root used when no firmware folder is passed in; must match the
# one run_Calibratron.py uses so both share one download.
AMBIT_FIRMWARE_CACHE = os.path.join(REPO_DIR, "firmware_cache")


def find_file(start_dir, filename):
    """Return the path to the first ``filename`` found in ``start_dir`` or any
    of its sub-folders, or None if it is nowhere to be found.
    """
    for root, _dirs, files in os.walk(start_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None


def read_flash_layout(firmware_dir, manifest_name=AMBIT_MANIFEST_NAME):
    """Read the esptool flash layout out of a firmware folder's manifest.

    The manifest's ``flash`` array is the contract with the firmware repo::

        {"flash": [{"file": "bootloader.bin", "offset": "0x0", "sha256": ...},
                   ...]}

    :param firmware_dir: folder holding ``manifest.json`` and the images
    :param manifest_name: manifest file name (override only for tests)
    :return: list of (offset, filename) tuples, ascending by offset
    :raises FileNotFoundError: if the manifest or one of its images is missing
    :raises RuntimeError: if the manifest is not usable JSON / has no layout
    """
    firmware_dir = Path(firmware_dir)
    manifest_path = firmware_dir / manifest_name
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"no {manifest_name} in {firmware_dir} - the firmware folder must be "
            f"one produced by firmware_fetch.fetch_latest()"
        ) from exc
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
        image = firmware_dir / str(name)
        if not image.is_file():
            raise FileNotFoundError(f"{manifest_path} lists {name!r} but {image} is missing")
        layout.append((str(offset), str(name)))

    # esptool does not care about the order, but a deterministic ascending
    # layout makes the logged command readable and diffable across runs.
    def _offset_value(item):
        try:
            return int(item[0], 0)
        except (TypeError, ValueError):
            return 0

    layout.sort(key=_offset_value)
    return layout


def esptool_command():
    """Return the argv prefix used to invoke esptool, cross-platform.

    Prefers the ``esptool.exe`` bundled in this repo on Windows (searched from
    this module's folder downwards - the firmware cache folder holds only the
    downloaded images, never the flasher); otherwise runs the installed
    ``esptool`` package via ``python -m esptool``.

    :raises RuntimeError: if no esptool is available
    """
    if os.name == "nt":
        local_exe = find_file(REPO_DIR, "esptool.exe")
        if local_exe:
            return [local_exe]
    if importlib.util.find_spec("esptool") is not None:
        return [sys.executable, "-m", "esptool"]
    raise RuntimeError(
        "esptool not found - install it with `pip install esptool`, "
        "or place esptool.exe in the Calibratron folder (Windows only)."
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


def flash_ambit(port, firmware_dir):
    """Write the Ambit firmware images in ``firmware_dir`` to the device on
    ``port`` by invoking esptool.

    The offsets and file names come from the folder's ``manifest.json`` (see
    :func:`read_flash_layout`). esptool is invoked with ``cwd=firmware_dir`` and
    bare file names, which keeps the command line short and free of the spaces
    that Windows bench paths are full of.

    :param port: serial port of the Ambit flasher
    :param firmware_dir: folder holding manifest.json + the firmware images
    :raises FileNotFoundError: if the manifest or one of its images is missing
    :raises RuntimeError: if esptool exits non-zero
    """
    # This is the lowest-level public flash entry point. Verify here so direct
    # callers cannot bypass public/immutable provenance and byte checks.
    import firmware_fetch
    if not firmware_fetch.is_complete(firmware_dir):
        raise RuntimeError(
            f"Firmware folder {firmware_dir} is not a complete verified "
            f"AMBIT release cache entry"
        )
    layout = read_flash_layout(firmware_dir)

    cmd = [
        *esptool_command(),
        "--chip", "esp32c3",
        "--baud", "921600",
        "--port", port,
        "--before", "default_reset",
        "--after", "hard_reset",
        "write_flash", "-z",
        "--flash_mode", "keep",
        "--flash_freq", "keep",
        "--flash_size", "keep",
    ]
    for offset, image in layout:
        cmd += [offset, image]

    logger.info("Flashing %s with esptool (%s)...", port,
                ", ".join(f"{off}:{img}" for off, img in layout))
    result = subprocess.run(cmd, cwd=str(firmware_dir))
    if result.returncode != 0:
        raise RuntimeError(f"esptool exited with return code {result.returncode}")
    logger.info("Flash completed.")


def flash_ambit_firmware(firmware_dir=None, *, cache_root=None, port=None,
                         force_flash=True):
    """Get the Ambit firmware, find the flasher port, and flash the device.

    :param firmware_dir: folder holding manifest.json + the firmware images
        (normally the ``firmware_cache/<version>/`` folder returned by
        ``firmware_fetch.fetch_latest``); if None, the latest public AMBIT release
        is fetched into ``cache_root`` first
    :param cache_root: firmware cache folder used when ``firmware_dir`` is None
        (default: ``<repo>/firmware_cache``)
    :param port: flasher serial port; if None, auto-detected (exactly one
        flasher must be connected)
    :param force_flash: when True, always flash; when False, flash only if a
        boot-time "invalid header" is detected on the device
    :return: True if the device was flashed, False if flashing was skipped
    :raises FileNotFoundError: if the manifest / images cannot be found
    :raises RuntimeError: if the firmware cannot be fetched, if zero / multiple
        flasher ports are found, or if esptool fails
    """
    if firmware_dir is None:
        # Imported lazily: only the flashing path needs it, and it must stay
        # usable even when this module is imported from a notebook kernel that
        # never flashes anything.
        import firmware_fetch
        _version, firmware_dir = firmware_fetch.fetch_latest(cache_root or AMBIT_FIRMWARE_CACHE)
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

    flash_ambit(port, firmware_dir)
    return True


# ============================================================================
# Post-flash hardware self-test (Ambit)
# ============================================================================
# Ported verbatim (logic and regexes) from the retired
# ``firmware_ambit/uploader.py``, which was the only place it lived. Nothing in
# the calibration flow calls it today; it is kept importable because it is the
# only automated check of the Ambit's sensor stack right after a flash, and it
# would otherwise be lost with the uploader script.

# MLX90632 temperature-sensor acceptance limits.
MIN_TEMP = -10             # deg C, lower bound for a valid reading
MAX_TEMP = 40              # deg C, upper bound for a valid reading
MLX_READ_TIME_LIMIT = 100  # ms, max acceptable sensor read time


def ambit_readlines(ser, timeout=1.0, invalid_bahave=False, max_lines=1, ending_line=""):
    """Read up to ``max_lines`` newline-terminated lines from ``ser``.

    Byte-at-a-time on purpose: the ``check`` dump is tab-formatted and the
    caller's regexes match on those exact tabs, and a byte >= 128 means the
    device is spewing framing noise rather than text (``invalid_bahave=True``
    aborts on that instead of poisoning the parse).

    :param ser: an open serial.Serial
    :param timeout: overall budget in seconds
    :param invalid_bahave: stop at the first non-ASCII byte
    :param max_lines: stop after this many lines
    :param ending_line: stop early once a line contains this substring
    :return: list of decoded lines (newline included)
    """
    lines = []
    line = ""
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < timeout:
        if ser.in_waiting > 0:
            r = ser.read()
            if r < bytes([128]):
                line += r.decode(errors="replace")
            else:
                if invalid_bahave:
                    break
            if r == b"\n":
                lines.append(line)
                line = ""
                if ending_line and ending_line in lines[-1]:
                    break
        else:
            time.sleep(0.1)
        if len(lines) >= max_lines:
            break
    return lines


def is_increasing(values):
    """True when each value in a sweep is larger than the one before it.

    The gain / current sweeps step the photodiode amplification up, so a
    healthy channel returns readings that climb monotonically.
    """
    return len(values) > 1 and all(b > a for a, b in zip(values, values[1:]))


def ambit_self_test(port):
    """Run the Ambit's built-in ``check`` self-test and grade every sensor.

    Detects the device with ``hello``, then drives ``check`` and parses the
    dump: ADPD chip id, AS7341 light level, MLX90632 timing + plausibility
    (cross-checked against the ESP32 die temperature), and the four photodiode
    gain / current sweeps.

    :param port: serial port of the Ambit device
    :return: dict of check name -> pass/fail bool
    """

    ret_dict = {"FW": False, "ADPD": False, "AS7341": False, "MLX90632": False,
                "Temp": False, "LightPass-SunPD": False, "LightPass-LeafPD": False,
                "LightPass-SignalPD": False, "LightPass-RefPD": False}

    ambit_ready = 0
    with serial.Serial(port, BAUDRATE) as ser:
        trials = 50
        logger.info("Trying to detect Ambit %d times", trials)
        ser.write(b"\r\n")
        ser.flush()
        time.sleep(0.1)

        for attempt in range(trials):
            # Drop any stale/buffered output (e.g. the boot log) before each
            # attempt, so we read the fresh reply to *this* hello instead of
            # chewing through a backlog one line at a time over all 50 trials.
            ser.reset_input_buffer()
            ser.write(b"hello\r\n")
            lines = ambit_readlines(ser, timeout=0.5, invalid_bahave=True, max_lines=1)
            logger.info("Reading: %s; %d/%d waiting for 'NEW Name Here Ready'...",
                        lines, attempt + 1, trials)
            if len(lines) == 0:
                continue

            if "NEW Name Here Ready" in lines[0]:
                logger.info("[PASS]\t\tAmbit is detected")
                ret_dict["FW"] = True
                ambit_ready = 1
                break

            if ambit_ready == 0:
                logger.info("Received: %s", lines[0].strip())

        if ambit_ready == 0:
            logger.info("[FAILED]\tAmbit detection failed")
            return ret_dict

        ser.write(b"check\r\n")
        lines = ambit_readlines(ser, timeout=5, invalid_bahave=False, max_lines=50,
                               ending_line="Done!!")

        adpd_match = re.compile(r"Checking ADPD\s+ADPD Found, chip version: (\d+)")
        as7341_match = re.compile(r"Checking AS7341\s+Success\s+(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
        mlx_match = re.compile(r"Checking MLX90632\s+Success\s+(\d+)\s+([\d.]+)\s+([\d.]+)")
        chip_match = re.compile(r"ESP32Temp\s+([\d.]+)")
        sunPD_match = re.compile(r"Sun PD\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")
        leafPD_match = re.compile(r"Leaf PD\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")
        signal_match = re.compile(r"Signal\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")
        ref_match = re.compile(r"Ref\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")

        light_intensity = 0
        chip_temp = -100.0
        temp1, temp2 = 100.0, 200.0

        for line in lines:
            if adpd_match.match(line):
                ret_dict["ADPD"] = True
                continue

            if chip_match.match(line):
                ret = chip_match.findall(line)
                if ret[0][0].isnumeric():
                    chip_temp = float(ret[0])
                continue

            if as7341_match.match(line):
                ret = as7341_match.findall(line)
                for n in ret[0]:
                    if n.isnumeric():
                        light_intensity += int(n)
                if light_intensity > 5:
                    ret_dict["AS7341"] = True
                    logger.info("[PASS]\t\tAS7341 Found, light intensity: %s", light_intensity)
                else:
                    logger.info("[FAILED]\tAS7341 Found, but light intensity too low: %s",
                                light_intensity)
                continue

            if mlx_match.match(line):
                ret = mlx_match.findall(line)
                read_time = int(ret[0][0])
                temp1 = float(ret[0][1])
                temp2 = float(ret[0][2])
                if read_time < MLX_READ_TIME_LIMIT and temp1 > MIN_TEMP and temp1 < MAX_TEMP and temp2 > MIN_TEMP and temp2 < MAX_TEMP:
                    ret_dict["MLX90632"] = True
                    logger.info("[PASS]\t\tMLX90632 Found, reading time:%s, die temp: %s, object temp: %s",
                                read_time, temp1, temp2)
                else:
                    if read_time >= MLX_READ_TIME_LIMIT:
                        logger.info("[FAILED]\tMLX90632 read time too long: %s", read_time)
                    else:
                        logger.info("[FAILED]\tMLX90632 Found, reading time:%s, die temp: %s, object temp: %s",
                                    ret[0][0], ret[0][1], ret[0][2])
                continue

            if sunPD_match.match(line):
                arr = [int(n) for n in sunPD_match.findall(line)[0]]
                logger.info("Sun PD values: %s", arr)
                if is_increasing(arr):
                    logger.info("[PASS]\t\t<SUN> PD gain sweep")
                    ret_dict["LightPass-SunPD"] = True
                else:
                    logger.info("[FAILED]\t<SUN> PD gain sweep not increasing!")
                continue

            if leafPD_match.match(line):
                arr = [int(n) for n in leafPD_match.findall(line)[0]]
                logger.info("Leaf PD values: %s", arr)
                if is_increasing(arr):
                    logger.info("[PASS]\t\t<Leaf> PD gain sweep")
                    ret_dict["LightPass-LeafPD"] = True
                else:
                    logger.info("[FAILED]\t<Leaf> PD gain sweep not increasing!")
                continue

            if signal_match.match(line):
                arr = [int(n) for n in signal_match.findall(line)[0]]
                logger.info("Signal PD values: %s", arr)
                if is_increasing(arr):
                    logger.info("[PASS]\t\t<Signal> PD Current sweep")
                    ret_dict["LightPass-SignalPD"] = True
                else:
                    logger.info("[FAILED]\t<Signal> PD Current sweep not increasing!")
                continue

            if ref_match.match(line):
                arr = [int(n) for n in ref_match.findall(line)[0]]
                logger.info("Ref PD values: %s", arr)
                if is_increasing(arr):
                    logger.info("[PASS]\t\t<Ref> PD Current sweep")
                    ret_dict["LightPass-RefPD"] = True
                else:
                    logger.info("[FAILED]\t<Ref> PD Current sweep not increasing!")
                continue

    if abs(chip_temp * 2 - temp1 - temp2) > 30:
        if ret_dict["MLX90632"]:
            logger.info("[FAILED]\tTemperature reading mismatch, chip temp: %s, mlx temp: %s, %s",
                        chip_temp, temp1, temp2)
    else:
        ret_dict["Temp"] = True

    return ret_dict


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


def plot_data_and_fit(x, y, coeffs, r2, output=None, xlabel="x", ylabel="y"):
    """
    Plot data points and linear fit with statistics.

    :param x: X values (array-like)
    :param y: Y values (array-like)
    :param coeffs: Polynomial coefficients from np.polyfit [slope, intercept]
    :param r2: R² value to display
    :param output: Optional file path to save the plot
    :param xlabel: Label for x-axis
    :param ylabel: Label for y-axis
    """
    plt.figure(figsize=(8, 5))
    plt.scatter(x, y, color="blue", label="Data points")

    x_sort = np.linspace(np.min(x), np.max(x), 300)
    y_fit = np.polyval(coeffs, x_sort)
    plt.plot(x_sort, y_fit, color="red",
             label=f"lin fit: {coeffs[0]:.4g}x + {coeffs[1]:.4g}   R² = {r2:.8g}")

    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.title("Data and Linear Fit")
    plt.grid(True)
    plt.legend()
    plt.tight_layout()

    if output:
        plt.savefig(output)
        print(f"Saved plot to {output}")

    plt.show()
