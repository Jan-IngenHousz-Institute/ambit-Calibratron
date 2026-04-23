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
import serial
import glob
from matplotlib import pyplot as plt
import numpy as np


# ============================================================================
# Device Discovery & Communication
# ============================================================================

def serial_ports():
    """
    Lists available serial port names for the current platform.

    :raises EnvironmentError: On unsupported or unknown platforms
    :returns: A list of the serial ports available on the system
    """
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
    return result


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
                    
                print(f"Received message: {msg.strip()}, port: {port}")

                if answer in msg:
                    print(f"Found device at: {port}, answer: {msg}")
                    return port
        except (OSError, serial.SerialException) as e:
            print(f"Error accessing {port}: {e}")
            continue

    print("No matching device found.")
    return None


def set_voltage(port, voltage):
    """Set voltage on DC source via serial port."""
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        msg = f"voltage {voltage:.3f}\r\n".encode()
        ser.write(msg)


def set_current(port, current):
    """Set current on DC source via serial port."""
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        msg = f"current {current:.3f}\r\n".encode()
        ser.write(msg)


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
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        cmd = "par_raw\n" if raw else "par\n"
        ser.write(cmd.encode())
        r = ser.readline()
        return float(r.decode().strip())


def get_par_AMB(port, raw=False):
    """
    Read PAR value from Ambit device.

    :param port: Serial port of the Ambit device
    :param raw: If True, request raw PAR value; if False, request calibrated value
    :return: PAR value as float
    """
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        _wait_for_device_ready(ser)
        cmd = "get_par\n" if raw else "PAR\n"
        ser.write(cmd.encode())
        r = ser.readline().decode(encoding='unicode_escape')
        return float(r)


# ============================================================================
# Calibration Functions
# ============================================================================

def _wait_for_device_ready(ser, expected_response=b"NEW", max_retries=10):
    """
    Wait for device to be ready by polling with 'hello' command.

    :param ser: Serial port object
    :param expected_response: Byte string to look for in response
    :param max_retries: Maximum number of retry attempts
    :return: The response received from device
    """
    resp = b""
    for _ in range(max_retries):
        ser.write("hello\n".encode())
        resp = ser.readline()
        # print(f"Response: {resp.decode(encoding='unicode_escape')}")
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
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        _wait_for_device_ready(ser)

        msg = f"set_spec, {coeff:.4f}\n".encode()
        ser.write(msg)
        time.sleep(0.2)

        _wait_for_device_ready(ser)


def set_ambit_led_gain(port, coeff):
    """
    Set LED calibration gain on Ambit device.

    :param port: Serial port of the Ambit device
    :param coeff: Calibration coefficient value
    """
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        _wait_for_device_ready(ser)

        msg = f"set_act, {coeff:.4f}\n".encode()
        ser.write(msg)
        time.sleep(0.2)

        _wait_for_device_ready(ser)




# ============================================================================
# Device Information & Management
# ============================================================================

class AmbitInfo:
    """Container for Ambit device information."""

    def __init__(self):
        self.FW = ""
        self.light_slope = 0.0
        self.IsValid = False
        self.name = ""
        self.act_led_coeff = 0.0

    def processInfo(self, line):
        """Parse device info from a line of serial response."""
        if b"FW:" in line and b"MAC" not in line:
            self.FW = line.split(b":")[1].strip()
        if b"Spec:" in line:
            self.light_slope = float(line.split(b"Spec:")[1].split(b'\t')[0].strip())
            self.IsValid = True
        if b"Name:" in line:
            self.name = line.split(b"Name:")[1].split(b'\t')[0].strip()
        if b"Actinic:" in line:
            self.act_led_coeff = float(line.split(b"Actinic:")[1].split(b'\t')[0].strip())

    def __str__(self):
        return (f"FW: {self.FW}, light_slope: {self.light_slope}, "
                f"IsValid: {self.IsValid}, name: {self.name}, "
                f"actinic gain: {self.act_led_coeff}")


def ambit_reboot(port):
    """
    Reboot Ambit device and retrieve its configuration information.

    :param port: Serial port of the Ambit device
    :return: AmbitInfo object with device configuration
    """
    info = AmbitInfo()

    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        ser.write("hello\n".encode())
        resp = ser.readline()
        ser.write("hello\n".encode())
        resp = ser.readline()

        while b"NEW" not in resp:
            ser.write("hello\n".encode())
            resp = ser.readline()
            #print("Response: " + resp.decode(encoding='unicode_escape'))

        ser.write("reboot\n".encode())

        # Process ambit data
        for i in range(26):
            l = ser.readline()
            info.processInfo(l)
            #print(l)
            if b"FW:" in l and b"MAC" not in l:
                info.IsValid = True
                break

    # Verify device is back online
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        ser.write("hello\n".encode())
        r = ser.readline()
        ser.write("hello\n".encode())
        r = ser.readline()
        #print(r)

    return info


# ============================================================================
# LED Control
# ============================================================================

def set_ambit_led(port, ledCurrent):
    """
    Set LED current on Ambit device and run measurement.

    :param port: Serial port of the Ambit device
    :param ledCurrent: LED current value (integer)
    """
    with serial.Serial(port, baudrate=115200) as ser:
        ser.flush()
        _wait_for_device_ready(ser)

        msg = f"arrun1,1,1,2,0,0,1,0,1,{ledCurrent:d},1,\n, \n".encode()
        ser.write(msg)
        time.sleep(0.2)

        #_wait_for_device_ready(ser)


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
