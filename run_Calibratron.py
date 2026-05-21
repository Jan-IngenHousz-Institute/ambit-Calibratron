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

import os, sys, json, time, re, importlib, subprocess, warnings
from datetime import datetime


def _ensure_requirements(req_file="requirements.txt"):
    """Check that the requirements.txt dependencies are installed, and pip-install
    any that are missing.

    Runs before the heavier third-party imports below, so the script can be
    launched on a fresh environment without a manual ``pip install`` step.
    Distributions are matched by name only (version specifiers are ignored for
    the check); if anything is missing, the whole requirements file is installed.

    :param req_file: requirements file name, resolved next to this script.
    """
    import importlib.metadata as md

    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, req_file)
    if not os.path.exists(path):
        print(f"[deps] {req_file} not found next to the script - skipping check")
        return

    installed = {name.lower() for name in md.packages_distributions()}
    installed |= {d.metadata["Name"].lower() for d in md.distributions()}

    missing = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()          # drop comments
            if not line:
                continue
            name = re.split(r"[<>=!~ \[]", line, 1)[0].strip().lower()
            if name and name not in installed:
                missing.append(name)

    if not missing:
        print("[deps] all requirements.txt dependencies present")
        return

    print(f"[deps] missing packages: {', '.join(missing)} - installing from {req_file}")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", path])
    print("[deps] dependencies installed")


_ensure_requirements()   # install missing deps before the third-party imports below

import numpy as np
import helpers; importlib.reload(helpers)

# ---- paths / tunables -----------------------------------------------------
# Anchor to the directory that contains helpers.py (== this script's folder).
# Using helpers.__file__ is robust in notebooks where this module's own
# __file__ may be a relative path and the kernel CWD is the workspace root,
# which would otherwise make os.path.abspath(__file__) point to the wrong dir.
HERE             = os.path.dirname(os.path.abspath(helpers.__file__))
FIRMWARE_DIR     = os.path.join(HERE, "firmware_ambit")   # Ambit firmware images to flash
CALIBRATIONS_DIR = os.path.join(HERE, "calibrations")   # where save_payload() writes

PAR_CAL_CURRENTS = [0.8, 2.4, 3.0, 4.0, 6.6, 0.0]   # A, DC source -> calibration lamp
# PAR_CAL_CURRENTS = [0.2, 0.4, 0.8, 1.0, 1.6, 0.0]   # A, DC source -> calibration lamp
LED_CAL_SETTINGS = [10, 20, 60, 90, 150, 250, 0]          # Ambit actinic LED steps
UPLOAD_GAINS     = True   # set False to preview the fit/plot without writing to the device
FORCE_FLASH_FIRMWARE   = False     # True -> always re-flash, regardless of current version
AMBIT_FW_VERSION       = "0.0.5"   # expected Ambit firmware; flash only if the device differs
RENAME_AMBIT = True


def _detect_ambit_version():
    """Discover an Ambit and return its firmware version string.

    :return: the firmware version (e.g. "0.0.5"), or None if no Ambit responds
        on any serial port.
    """
    helpers._invalidate_port_cache()   # COM topology may have changed
    port = helpers.findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)
    if port is None:
        return None
    fw = helpers.ambit_reboot(port).FW
    return fw.decode(errors="replace").strip() if fw else None


def flash_firmware(force_flash=False, version=None):
    """(Re)flash the Ambit firmware via helpers.flash_ambit_firmware().

    Uses the firmware images in FIRMWARE_DIR; no external uploader script is
    involved. helpers.flash_ambit_firmware() locates the flasher COM port,
    opens it for esptool, and closes it again, so the port is free for
    discovery afterwards.

    Flashing is decided as follows:
      - ``force_flash=True`` -> always flash, regardless of the current version.
      - ``version`` given    -> detect the Ambit's current firmware and flash
        only when it differs from ``version``, or when no Ambit is detected.
        After flashing, the device is rebooted and a warning is raised if the
        running version still isn't ``version``.
      - neither given        -> flash only when an "invalid header" is detected
        on boot.

    :param force_flash: re-flash even when the current firmware looks valid;
        otherwise flashing happens only on a version mismatch / invalid header.
    :param version: expected firmware version string (e.g. "0.0.5"). When set,
        the Ambit is flashed only if its current firmware differs from this.
    :return: 0 on success (including a deliberately skipped flash), 1 on failure.
    """
    # Decide whether the Ambit needs flashing, and whether to force it
    # (a version mismatch flashes even with force_flash=False).
    should_force = force_flash
    if force_flash:
        print("[flash] force_flash=True - flashing regardless of current version")
    elif version is not None:
        current = _detect_ambit_version()
        if current is None:
            print(f"[flash] no Ambit detected - flashing firmware {version}")
            should_force = True
        elif current == version:
            print(f"[flash] Ambit already runs firmware {current} - skipping flash")
            return 0
        else:
            print(f"[flash] Ambit runs firmware {current!r}, expected {version!r} - flashing")
            should_force = True
    else:
        print("[flash] no target version - flashing only on invalid header")

    try:
        flashed = helpers.flash_ambit_firmware(firmware_dir=FIRMWARE_DIR,
                                               force_flash=should_force)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[flash] flashing failed: {exc}")
        return 1
    print(f"[flash] {'firmware flashed' if flashed else 'flash skipped'}")

    # Verify the freshly-flashed firmware matches the expected version.
    if version is not None and flashed:
        time.sleep(1.0)   # let the device finish rebooting
        running = _detect_ambit_version()
        if running != version:
            warnings.warn(f"Firmware flashed NOT the one expected "
                          f"(running {running!r}, expected {version!r})")
        else:
            print(f"[flash] verified firmware {running}")

    return 0


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
    if FORCE_FLASH_FIRMWARE:
        print("WARNING: FORCE_FLASH_FIRMWARE is True - the device will be re-flashed even if it already runs the expected firmware")
        print("=== Flashing firmware ===")
        rc = flash_firmware(force_flash=FORCE_FLASH_FIRMWARE, version=AMBIT_FW_VERSION)
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

    # 5. Flash new version
    current_fw = info_precalibration.FW.decode(errors="replace").strip()
    if AMBIT_FW_VERSION and current_fw != AMBIT_FW_VERSION:
        print(f"\n=== Firmware version mismatch: running {current_fw!r}, expected {AMBIT_FW_VERSION!r} ===")
        rc = flash_firmware(force_flash=FORCE_FLASH_FIRMWARE, version=AMBIT_FW_VERSION)
        if rc != 0:
            raise SystemExit(f"Firmware flashing failed (uploader.py exit code {rc})")
        time.sleep(1.0)            # let the device finish rebooting
        helpers._invalidate_port_cache()   # COM topology may have changed

    # 6. Rename ambit
    if RENAME_AMBIT:
        current_name = info_precalibration.name.decode(errors="replace").strip()
        new_name = input(f"Enter new name for Ambit (current: {current_name}): ").strip()
        helpers.set_ambit_name(port_ambit, new_name)

    # 7. PAR-sensor calibration (needs the DC source + PAR-reference MiniPAR)
    if port_ref and port_dc:
        print("\n=== PAR sensor calibration ===")
        calibrate_par_sensor(port_ambit, port_ref, port_dc)
    else:
        missing = ", ".join(n for n, p in (("Par_REF MiniPAR", port_ref),
                                           ("Kiprim DC source", port_dc)) if p is None)
        print(f"\n[skip] PAR sensor calibration - missing: {missing}")

    # 8. Actinic-LED calibration (needs the Emit_LED MiniPAR)
    if port_emit:
        print("\n=== Actinic LED calibration ===")
        calibrate_led(port_ambit, port_emit)
    else:
        print("\n[skip] Actinic LED calibration - missing: Emit_LED MiniPAR")

    # 9. Final state
    print("\n=== Ambit after calibration ===")
    info_postcalibration = helpers.ambit_reboot(port_ambit)
    print(info_postcalibration)

    # 10. Build the calibration payload (aborts with a warning if either dump is empty)
    print("\n=== Calibration payload ===")
    payload = helpers.make_calibration_payload(info_precalibration, info_postcalibration)
    print(payload)

    # 11. Save the payload to ./calibrations/<YYYY-MM-DD_HH-MM-SS>_<MAC>.json
    print("\n=== Saving payload ===")
    save_payload(payload, mac=info_postcalibration.MAC)
    # return payload

    # 12. (Optional) Publish the payload to AWS IoT Core via MQTT
    helpers.publish_payload_mqtt5(
        payload,
        topic="experiment/data_ingest/v1/993ae58e-2e87-45ef-96e1-5bbdb0916817/ambit/v1.0/ambit_calibration_1/1234556",
        certs_dir="ambit_calibration_1_certs/ambit_calibration_1_certs",
        endpoint="http://a3qrmjf5m5y241-ats.iot.eu-central-1.amazonaws.com",   # your AWS IoT ATS endpoint
    )


if __name__ == "__main__":
    main()
