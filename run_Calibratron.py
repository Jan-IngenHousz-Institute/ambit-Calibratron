"""Calibratron runner.

Discovers an Ambit, dumps its config, then runs the PAR-sensor and actinic-LED
calibrations against reference instruments on the bench.

The calibration steps need extra hardware connected:
  - a Kiprim DC source            -> answers "KIPRIM"  to "*IDN?"
  - a MiniPAR used as PAR reference -> answers "Par_REF" to "get_name"
  - a MiniPAR over the actinic LED  -> answers "Emit_LED" to "get_name"
Whatever isn't connected is reported and that calibration step is skipped, so
the script is still useful with only the Ambit plugged in.
"""

import os, sys, json, subprocess, time, importlib
from datetime import datetime
import numpy as np
import helpers; importlib.reload(helpers)

# ---- paths / tunables -----------------------------------------------------
# Anchor to the directory that contains helpers.py (== this script's folder).
# Using helpers.__file__ is robust in notebooks where this module's own
# __file__ may be a relative path and the kernel CWD is the workspace root,
# which would otherwise make os.path.abspath(__file__) point to the wrong dir.
HERE             = os.path.dirname(os.path.abspath(helpers.__file__))
UPLOADER         = os.path.join(HERE, "firmware_ambit", "uploader.py")   # firmware flasher
CALIBRATIONS_DIR = os.path.join(HERE, "calibrations")   # where save_payload() writes

PAR_CAL_CURRENTS = [0.2, 0.4, 0.8, 1.0, 1.6, 0.0]   # A, DC source -> calibration lamp
LED_CAL_SETTINGS = [10, 20, 60, 90, 150, 0]          # Ambit actinic LED steps
UPLOAD_GAINS     = True   # set False to preview the fit/plot without writing to the device
FORCE_FLASH_FIRMWARE   = False   # set False to skip the uploader.py flashing step in main()


def flash_firmware(force_flash=False, run_test=False):
    """Run firmware_ambit/uploader.py to (re)flash the Ambit firmware.

    The uploader chdir's to its own folder, opens the flasher COM port, and
    closes it again before returning, so the serial port is free for discovery
    afterwards.

    :param force_flash: pass --force-flash=true to re-flash even when the
        current firmware looks valid; otherwise the uploader only flashes when
        it detects an "invalid header" on boot.
    :param run_test: pass --run-test=true to run the uploader's post-flash
        sensor self-test.
    :return: the uploader's exit code (0 = success)
    """
    if not os.path.isfile(UPLOADER):
        raise FileNotFoundError(f"uploader.py not found at {UPLOADER!r}")
    cmd = [
        sys.executable, UPLOADER,
        f"--force-flash={'true' if force_flash else 'false'}",
        f"--run-test={'true' if run_test else 'false'}",
    ]
    print(f"[flash] {' '.join(cmd)}")
    rc = subprocess.run(cmd, cwd=os.path.dirname(UPLOADER)).returncode
    print(f"[flash] uploader.py exited with code {rc}")
    return rc


def save_payload(payload, mac=None, directory=CALIBRATIONS_DIR):
    """Write the calibration payload to '<YYYY-MM-DD_HH-MM-SS>_<MAC>.json'.

    :param payload: the JSON string (or dict) from helpers.make_calibration_payload
    :param mac: device MAC for the filename; if None, read from payload["device_id"]
    :param directory: target folder (created if missing); defaults to ./calibrations
    :return: the path of the file written
    """
    data = json.loads(payload) if isinstance(payload, str) else payload
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    mac  = mac or data.get("device_id") or "UNKNOWN"
    fname = f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{mac}.json"
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[save] wrote {path}")
    return path


def calibrate_par_sensor(port_ambit, port_ref, port_dc, currents=PAR_CAL_CURRENTS, upload=UPLOAD_GAINS):
    """Sweep the calibration lamp, fit Ambit-raw PAR vs MiniPAR reference, show
    the plot, and (optionally) upload the slope as the Ambit PAR gain.

    :return: (slope, r2)
    """
    ref_par, ambit_raw = [], []
    for I in currents:
        helpers.set_current(port=port_dc, current=I)
        time.sleep(1.0)
        ref_par.append(helpers.get_par_MP(port_ref))
        ambit_raw.append(helpers.get_par_AMB(port_ambit, raw=True))
    helpers.set_current(port=port_dc, current=0.0)

    x, y = np.array(ambit_raw), np.array(ref_par)
    coeffs = np.polyfit(x, y, 1)
    r2 = helpers.r_squared(y, np.polyval(coeffs, x))
    slope = float(coeffs[0])

    old = helpers.ambit_reboot(port_ambit).light_slope
    print(f"[PAR cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current light_slope={old:.4f})")
    helpers.plot_data_and_fit(x, y, coeffs, r2,
                              xlabel="Ambit PAR (raw)", ylabel="MiniPAR PAR (reference)")

    if upload:
        helpers.set_par_gain(port_ambit, slope)
        new = helpers.ambit_reboot(port_ambit).light_slope
        print(f"[PAR cal] uploaded PAR gain: {old:.4f} -> {new:.4f}")
    return slope, r2


def calibrate_led(port_ambit, port_emit, settings=LED_CAL_SETTINGS, upload=UPLOAD_GAINS):
    """Sweep the Ambit actinic LED, fit measured PAR vs LED setting, show the
    plot, and (optionally) upload the slope as the Ambit LED gain.

    :return: (slope, r2)
    """
    led_setting, measured = [], []
    for s in settings:
        helpers.set_ambit_led(port_ambit, s)
        time.sleep(0.2)
        measured.append(helpers.get_par_MP(port_emit))
        led_setting.append(s)

    x, y = np.array(measured), np.array(led_setting)
    coeffs = np.polyfit(x, y, 1)
    r2 = helpers.r_squared(y, np.polyval(coeffs, x))
    slope = float(coeffs[0])

    old = helpers.ambit_reboot(port_ambit).act_led_coeff

    if r2 < 0.99:
        print("[LED cal] WARNING: poor fit quality - check the plot for outliers or nonlinearity")
        helpers.plot_data_and_fit(x, y, coeffs, r2,
                                xlabel="MiniPAR PAR (over LED)", ylabel="Ambit LED setting")
        print("[LED cal] uploading old values due to poor fit quality")
        return old, r2

    print(f"[LED cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current act_led_coeff={old:.4f})")
    if upload:
        helpers.set_ambit_led_gain(port_ambit, slope)
        new = helpers.ambit_reboot(port_ambit).act_led_coeff
        print(f"[LED cal] uploaded LED gain: {old:.4f} -> {new:.4f}")
    return slope, r2


def main():

    # 0. Flash the Ambit firmware via firmware_ambit/uploader.py
    print("=== Flashing firmware ===")
    rc = flash_firmware(force_flash=FORCE_FLASH_FIRMWARE)
    if rc != 0:
        raise SystemExit(f"Firmware flashing failed (uploader.py exit code {rc})")
    time.sleep(1.0)            # let the device finish rebooting
    helpers._invalidate_port_cache()   # COM topology may have changed

    # 1. Cache check: second serial_ports() call should be ~instant
    t0 = time.perf_counter(); helpers.serial_ports(); t1 = time.perf_counter()
    helpers.serial_ports(); t2 = time.perf_counter()
    print(f"serial_ports first: {t1-t0:.3f}s, cached: {t2-t1:.5f}s")

    # 2. Discover the Ambit (retry once)
    port_ambit = helpers.findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)
    if port_ambit is None:
        port_ambit = helpers.findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)
    if port_ambit is None:
        raise SystemExit("No Ambit device found on any serial port")

    # 3. Reboot the device and parse its config dump into an AmbitInfo
    info_precalibration = helpers.ambit_reboot(port_ambit)
    print(info_precalibration)

    # 4. Discover reference instruments
    port_ref  = helpers.findDevice(question="get_name\n", answer="Par_REF",  flush=True, timeout=2)
    port_emit = helpers.findDevice(question="get_name\n", answer="Emit_LED", flush=True, timeout=2)
    port_dc   = helpers.findDevice(question="*IDN?\n",    answer="KIPRIM",   flush=True, timeout=2)

    # 5. PAR-sensor calibration (needs the DC source + PAR-reference MiniPAR)
    if port_ref and port_dc:
        print("\n=== PAR sensor calibration ===")
        calibrate_par_sensor(port_ambit, port_ref, port_dc)
    else:
        missing = ", ".join(n for n, p in (("Par_REF MiniPAR", port_ref),
                                           ("Kiprim DC source", port_dc)) if p is None)
        print(f"\n[skip] PAR sensor calibration - missing: {missing}")

    # 6. Actinic-LED calibration (needs the Emit_LED MiniPAR)
    if port_emit:
        print("\n=== Actinic LED calibration ===")
        calibrate_led(port_ambit, port_emit)
    else:
        print("\n[skip] Actinic LED calibration - missing: Emit_LED MiniPAR")

    # 7. Final state
    print("\n=== Ambit after calibration ===")
    info_postcalibration = helpers.ambit_reboot(port_ambit)
    print(info_postcalibration)

    # 8. Build the calibration payload (aborts with a warning if either dump is empty)
    print("\n=== Calibration payload ===")
    payload = helpers.make_calibration_payload(info_precalibration, info_postcalibration)
    print(payload)

    # 9. Save the payload to ./calibrations/<YYYY-MM-DD_HH-MM-SS>_<MAC>.json
    print("\n=== Saving payload ===")
    save_payload(payload, mac=info_postcalibration.MAC)
    # return payload

    # 10. (Optional) Publish the payload to AWS IoT Core via MQTT
    helpers.publish_payload_mqtt5(
        payload,
        topic="experiment/data_ingest/v1/993ae58e-2e87-45ef-96e1-5bbdb0916817/ambit/v1.0/ambit_calibration_1/1234556",
        certs_dir="ambit_calibration_1_certs/ambit_calibration_1_certs",
        endpoint="http://a3qrmjf5m5y241-ats.iot.eu-central-1.amazonaws.com",   # your AWS IoT ATS endpoint
    )


if __name__ == "__main__":
    main()
