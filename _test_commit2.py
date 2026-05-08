"""Temporary verification script for Commit 2. Delete after use."""
import sys
import time
import logging

import helpers
logging.getLogger("helpers").setLevel(logging.DEBUG)
print("helpers from:", helpers.__file__)
from helpers import (
    findDevice, get_par_MP, get_par_AMB,
    set_par_gain, ambit_reboot,
    AmbitProto, MiniParProto, DCSourceProto,
)

# 1. Discover MiniPAR (PAR sensor)
PORT_MINIPAR_PAR = findDevice(question="get_name\n", answer="Par_REF", flush=True, timeout=2)
if PORT_MINIPAR_PAR is None:
    sys.exit("MiniPAR PAR not found")

# 2. Let the Ambit settle — every preceding port-open during MiniPAR discovery
#    may have reset it via DTR-on-open.
print("\n--- Settling 3s before Ambit discovery ---")
time.sleep(3.0)

# Discover Ambit (with retry — pre-existing cold-boot quirk)
PORT_AMBIT = findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)
if PORT_AMBIT is None:
    print("--- First Ambit attempt failed; settling 3s and retrying ---")
    helpers._invalidate_port_cache()
    time.sleep(3.0)
    PORT_AMBIT = findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)
if PORT_AMBIT is None:
    sys.exit("Ambit not found")

print(f"\n--- Ports: MiniPAR={PORT_MINIPAR_PAR}, Ambit={PORT_AMBIT} ---\n")

# Give Ambit a moment to settle after discovery's port-open reset
time.sleep(1.0)

# 3. PAR readouts
print("MP raw:", get_par_MP(PORT_MINIPAR_PAR, raw=True))
print("AMB raw:", get_par_AMB(PORT_AMBIT, raw=True))

# 4. set_par_gain roundtrip — read original, set test value, verify, restore
info = ambit_reboot(PORT_AMBIT)
original_slope = info.light_slope
print(f"\nOriginal light_slope: {original_slope:.4f}")

TEST_VALUE = 0.1892
set_par_gain(PORT_AMBIT, TEST_VALUE)
info = ambit_reboot(PORT_AMBIT)
print(f"After set_par_gain({TEST_VALUE}): {info.light_slope:.4f}")
assert abs(info.light_slope - TEST_VALUE) < 1e-4, \
    f"set_par_gain regressed: expected ~{TEST_VALUE}, got {info.light_slope}"

# Restore original
set_par_gain(PORT_AMBIT, original_slope)
info = ambit_reboot(PORT_AMBIT)
print(f"Restored: {info.light_slope:.4f}")
assert abs(info.light_slope - original_slope) < 1e-4, "restore failed"

print("\n=== Commit 2 verification: OK ===")
