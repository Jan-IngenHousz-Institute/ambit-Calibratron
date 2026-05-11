"""
Helper functions for Ambit calibration and PAR measurements.

This module contains utilities for:
- Serial device communication and discovery
- PAR (Photosynthetically Active Radiation) measurements
- Data analysis and visualization
- Device calibration
"""

import sys
import time
import json
import logging
import serial
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


def findDevice(question="hello", answer="", flush=True, timeout=5):
    """
    Find Ambit device on available serial ports by handshake.

    Attempts to find a device by sending a 'question' string and looking for
    an 'answer' substring in the response.

    :param question: The message to send to the device (default: "hello")
    :param answer: The substring expected in the device response
    :param flush: Whether to flush the serial buffer before sending (default: True)
    :param timeout: The read timeout for the serial port in seconds (default: 5)
    :return: The port where the device was found, or None if not found
    """
    for port in serial_ports():
        try:
            with serial.Serial(port, baudrate=115200, timeout=timeout) as ser:
                # Windows-specific: Set DTR and RTS signals (needed for some devices)
                ser.dtr = True
                ser.rts = True
                
                if flush:
                    ser.flush()
                    ser.reset_input_buffer()
                    ser.reset_output_buffer()
                    time.sleep(0.5)
                
                # Extra delay on Windows to allow device to respond after signals set
                if sys.platform.startswith('win'):
                    time.sleep(0.3)
                    
                ser.write(question.encode())
                time.sleep(0.8)  # Increased timing
                
                # Use read_all() instead of readline() to handle responses without newlines
                msg_bytes = ser.read_all()
                
                # Decode with unicode_escape encoding for special characters
                try:
                    msg = msg_bytes.decode(encoding='unicode_escape')
                except:
                    msg = msg_bytes.decode(errors='replace')
                    
                logger.debug("Received message: %s, port: %s", msg.strip(), port)

                if answer in msg:
                    logger.info("Found device at: %s, answer: %s", port, msg)
                    return port
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
    HELLO       = "hello\n"
    HELLO_ACK   = b"NEW"
    REBOOT      = "reboot\n"
    GET_PAR_RAW = "get_par\n"
    GET_PAR_CAL = "PAR\n"
    SET_SPEC    = "set_spec, {coeff:.4f}\n"
    SET_ACT     = "set_act, {coeff:.4f}\n"
    LED_RUN     = "arrun1,1,1,2,0,0,1,0,1,{led:d},1,\n, \n"


class MiniParProto:
    """Wire protocol for the MiniPAR device."""
    GET_PAR_RAW = "par_raw\n"
    GET_PAR_CAL = "par\n"
    GET_NAME    = "get_name\n"


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


def make_calibration_payload(info_precalibration=None, info_postcalibration=None, *,
                             device_id=None, device_name=None,
                             firmware_version=None, device_firmware=None,
                             device_version="1", protocol_id="CALIBRATION",
                             indent=2):
    """Build the JSON calibration-upload payload from the pre/post AmbitInfo dumps.

    The ``device_*`` / ``firmware_version`` fields default to values read from
    ``info_postcalibration`` (falling back to ``info_precalibration``); pass
    explicit strings to override any of them.

    :param info_precalibration: AmbitInfo captured before calibration
    :param info_postcalibration: AmbitInfo captured after calibration
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
# LED Control
# ============================================================================

def set_ambit_led(port, ledCurrent):
    """
    Set LED current on Ambit device and run measurement.

    :param port: Serial port of the Ambit device
    :param ledCurrent: LED current value (integer)
    """
    _ambit_command(
        port,
        AmbitProto.LED_RUN.format(led=ledCurrent),
        verify_ready=False,
    )


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
