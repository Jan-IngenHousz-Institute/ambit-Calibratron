import sys
import time
import serial
import glob
from matplotlib import pyplot as plt
import numpy as np
from helpers import (
    findDevice, set_voltage, set_current, get_par_MP, get_par_AMB,
    r_squared, plot_data_and_fit, set_par_gain, set_ambit_led_gain,
    set_ambit_led, ambit_reboot, AmbitInfo
)


# Initialize ports for miniPAR sensing PAR and sensing LED emission.
PORT_MINIPAR_PAR = findDevice(question="get_name\n",answer="Par_REF",flush=True,timeout=2)
PORT_MINIPAR_EMIT = findDevice(question="get_name\n",answer="Emit_LED",flush=True,timeout=2)
# Initialize ports for DC supply
PORT_DC_SOURCE = findDevice(question="*IDN?\n", answer="KIPRIM", flush=True, timeout=2)

# Initialize ports for Ambit
PORT_AMBIT = findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)
if(PORT_AMBIT is None):
    PORT_AMBIT = findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)


if PORT_MINIPAR_PAR is None:
    print("miniPAR PAR device not found. Please check the connection and try again.")
    sys.exit(1)

if PORT_MINIPAR_EMIT is None:
    print("miniPAR LED device not found. Please check the connection and try again.")
    sys.exit(1)

if PORT_DC_SOURCE is None:
    print("DC source device not found. Please check the connection and try again.")
    sys.exit(1)

if PORT_AMBIT is None:
    print("Ambit device not found. Please check the connection and try again.")
    sys.exit(1)


# calibration of the light sensor in the Ambit
currents = [0.2, 0.4, 0.8, 1.0, 1.6, 0]
reference_data = []
sensor_data = []
for I in currents:
    set_current(port=PORT_DC_SOURCE, current=I)
    time.sleep(1.0)
    ref_val = get_par_MP(PORT_MINIPAR_PAR)
    sens_val = get_par_AMB(PORT_AMBIT, raw=True)
    reference_data.append(ref_val)
    sensor_data.append(sens_val)

# Analysis functions
# (r_squared and plot_data_and_fit are now imported from helpers.py)
y = reference_data
x = sensor_data
coeffs = np.polyfit(x, y, 1)
y_pred = np.polyval(coeffs, x)
r2 = r_squared(y, y_pred)


# info about the fit
info = ambit_reboot(PORT_AMBIT)
print("Old ambit PAR coefficient:", info.act_led_coeff)
plot_data_and_fit(x, y, coeffs, r2, xlabel="Ambit PAR", ylabel="MiniPAR PAR")


# Upload PAR calibration coefficients to the Ambit
# This section demonstrates how to use set_par_gain
from helpers import set_par_gain, ambit_reboot, AmbitInfo

# Example:
slope = coeffs[0]
#offset = coeffs[1]
info = ambit_reboot(PORT_AMBIT)
old_slope =info.light_slope
print(f"Uploading PAR gain: {slope:.4f}")
set_par_gain(PORT_AMBIT, slope)
info = ambit_reboot(PORT_AMBIT)

print(f"Old PAR gain: {old_slope:.4f}, New PAR gain: {info.light_slope:.4f}") 