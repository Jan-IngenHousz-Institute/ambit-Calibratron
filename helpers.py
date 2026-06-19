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


class MiniParProto:
    """Wire protocol for the MiniPAR device."""
    GET_PAR_RAW = "par_raw\n"
    GET_PAR_CAL = "par\n"
    GET_NAME    = "get_name\n"
    SET_NAME    = "set_name,{name}\n"


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


def get_par_AMB(port, raw=False):
    """
    Read PAR value from Ambit device.

    :param port: Serial port of the Ambit device
    :param raw: If True, request raw PAR value; if False, request calibrated value
    :return: PAR value as float
    """
    cmd = AmbitProto.GET_PAR_RAW if raw else AmbitProto.GET_PAR_CAL
    return float(_ambit_query(port, cmd))


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

# esptool flash offset -> image, for the ESP32-C3 on the Ambit.
_AMBIT_FLASH_LAYOUT = [
    ("0x0",     "ambit-1.ino.bootloader.bin"),
    ("0x8000",  "ambit-1.ino.partitions.bin"),
    ("0xe000",  "boot_app0.bin"),
    ("0x10000", "ambit-1.ino.bin"),
]


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


def flash_ambit(port, firmware_dir):
    """Write the Ambit firmware images in ``firmware_dir`` to the device on
    ``port`` by invoking esptool.

    :param port: serial port of the Ambit flasher
    :param firmware_dir: folder holding the AMBIT_FIRMWARE_FILES images
    :raises RuntimeError: if esptool exits non-zero
    """
    cmd = [
        *esptool_command(firmware_dir),
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
    for offset, image in _AMBIT_FLASH_LAYOUT:
        cmd += [offset, image]

    logger.info("Flashing %s with esptool...", port)
    result = subprocess.run(cmd, cwd=firmware_dir)
    if result.returncode != 0:
        raise RuntimeError(f"esptool exited with return code {result.returncode}")
    logger.info("Flash completed.")


def flash_ambit_firmware(firmware_dir=None, *, search_root=None, port=None,
                         force_flash=True):
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

    flash_ambit(port, firmware_dir)
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
