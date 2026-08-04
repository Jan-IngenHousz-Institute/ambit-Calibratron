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

import os, sys, json, time, re, importlib, subprocess, warnings, urllib.error
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
import firmware_fetch; importlib.reload(firmware_fetch)

# ---- paths / tunables -----------------------------------------------------
# Anchor to the directory that contains helpers.py (== this script's folder).
# Using helpers.__file__ is robust in notebooks where this module's own
# __file__ may be a relative path and the kernel CWD is the workspace root,
# which would otherwise make os.path.abspath(__file__) point to the wrong dir.
HERE             = os.path.dirname(os.path.abspath(helpers.__file__))
# Downloaded Ambit firmware releases land here as firmware_cache/<version>/
# (manifest.json + images); git-ignored, populated by firmware_fetch.
FIRMWARE_CACHE_DIR = os.path.join(HERE, "firmware_cache")
CALIBRATIONS_DIR = os.path.join(HERE, "calibrations")   # where save_payload() writes

PAR_CAL_CURRENTS = [0.8, 2.4, 3.0, 4.0, 6.6, 0.0]   # A, DC source -> calibration lamp
# PAR_CAL_CURRENTS = [0.2, 0.4, 0.8, 1.0, 1.6, 0.0]   # A, DC source -> calibration lamp
LED_CAL_SETTINGS = [10, 20, 60, 90, 150, 250, 0]          # Ambit actinic LED steps
UPLOAD_GAINS     = True   # set False to preview the fit/plot without writing to the device
FORCE_FLASH_FIRMWARE   = False     # True -> always re-flash, even if the device is up to date
RENAME_AMBIT = True

# The expected firmware version is no longer pinned here: it is whatever the
# latest published ambit-iot release says (see firmware_fetch.fetch_latest).

# Ambit firmware >= 0.1.0 answers `hello` with "NEW <name> Ready FW:<version>",
# so the version can be read without the (slower, reboot-triggering) boot dump.
_HELLO_FW_RE = re.compile(r"FW:([0-9][^\s]+)")


def _detect_ambit_version():
    """Discover an Ambit and return its firmware version string.

    Reads the version from the ``hello`` reply when the device is new enough to
    include it, and falls back to the boot dump for older firmware, which only
    prints ``FW: x.y.z`` while rebooting.

    :return: the firmware version (e.g. "0.1.0"), or None if no Ambit responds
        on any serial port.
    """
    helpers._invalidate_port_cache()   # COM topology may have changed
    port = helpers.findDevice(question="hello\n", answer="NEW", flush=True, timeout=4)
    if port is None:
        return None

    try:
        reply = helpers._ambit_query(port, helpers.AmbitProto.HELLO)
    except Exception as exc:                      # serial hiccup: try the boot dump
        print(f"[flash] could not read the hello reply on {port}: {exc}")
        reply = ""
    match = _HELLO_FW_RE.search(reply or "")
    if match:
        return match.group(1).strip()

    # Old firmware: no version in the hello reply, only in the boot dump.
    fw = helpers.ambit_reboot(port).FW
    return fw.decode(errors="replace").strip() if fw else None


def flash_firmware(force_flash=False, current_version=None, cache_root=FIRMWARE_CACHE_DIR):
    """Fetch the latest published Ambit firmware and flash it if needed.

    The images come from the newest ambit-iot GitHub release, downloaded into
    ``cache_root/<version>/`` by firmware_fetch (which falls back to the newest
    complete cache entry when GitHub is unreachable). helpers.flash_ambit_firmware()
    locates the flasher COM port, opens it for esptool, and closes it again, so
    the port is free for discovery afterwards.

    Flashing is decided as follows:
      - ``force_flash=True``            -> always flash.
      - device version != release       -> flash (also when no Ambit answers).
      - device version == release       -> skip.
    After a flash the device is re-read and a warning is raised if it still is
    not running the release version.

    :param force_flash: re-flash even when the device is already up to date.
    :param current_version: firmware version already read from the device; when
        None it is detected over serial.
    :param cache_root: firmware cache folder to download into.
    :return: 0 on success (including a deliberately skipped flash), 1 on failure.
    """
    try:
        version, firmware_dir = firmware_fetch.fetch_latest(cache_root)
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"[flash] could not obtain the Ambit firmware: {exc}")
        return 1
    print(f"[flash] latest published firmware: {version} ({firmware_dir})")

    # Decide whether the Ambit needs flashing, and whether to force it
    # (a version mismatch flashes even with force_flash=False).
    should_force = force_flash
    if force_flash:
        print("[flash] force_flash=True - flashing regardless of current version")
    else:
        current = current_version or _detect_ambit_version()
        if current is None:
            print(f"[flash] no Ambit detected - flashing firmware {version}")
            should_force = True
        elif current == version:
            print(f"[flash] Ambit already runs firmware {current} - skipping flash")
            return 0
        else:
            print(f"[flash] Ambit runs firmware {current!r}, latest is {version!r} - flashing")
            should_force = True

    try:
        flashed = helpers.flash_ambit_firmware(firmware_dir=firmware_dir,
                                               force_flash=should_force)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[flash] flashing failed: {exc}")
        return 1
    print(f"[flash] {'firmware flashed' if flashed else 'flash skipped'}")

    # Verify the freshly-flashed firmware matches the release we just fetched.
    if flashed:
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

    :return: a JSON-ready dict with the sweep (x/y arrays + axis labels), the
        fitted slope and r2.
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

    cal = {
        "x": x.tolist(), "x_label": "Ambit PAR (raw)",
        "y": y.tolist(), "y_label": "MiniPAR PAR (reference)",
        "slope": slope, "r2": float(r2),
    }

    old = helpers.ambit_reboot(port_ambit).light_slope
    print(f"[PAR cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current light_slope={old:.4f})")
    helpers.plot_data_and_fit(x, y, coeffs, r2,
                              xlabel="Ambit PAR (raw)", ylabel="MiniPAR PAR (reference)")

    if upload:
        helpers.set_par_gain(port_ambit, slope)
        new = helpers.ambit_reboot(port_ambit).light_slope
        print(f"[PAR cal] uploaded PAR gain: {old:.4f} -> {new:.4f}")
    return cal


def calibrate_led(port_ambit, port_emit, settings=LED_CAL_SETTINGS, upload=UPLOAD_GAINS):
    """Sweep the Ambit actinic LED, fit measured PAR vs LED setting, show the
    plot, and (optionally) upload the slope as the Ambit LED gain.

    :return: a JSON-ready dict with the sweep (x/y arrays + axis labels), the
        fitted slope and r2.
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

    cal = {
        "x": x.tolist(), "x_label": "MiniPAR PAR (over LED)",
        "y": y.tolist(), "y_label": "Ambit LED setting",
        "slope": slope, "r2": float(r2),
    }

    old = helpers.ambit_reboot(port_ambit).act_led_coeff

    if r2 < 0.99:
        print("[LED cal] WARNING: poor fit quality - check the plot for outliers or nonlinearity")
        helpers.plot_data_and_fit(x, y, coeffs, r2,
                                xlabel="MiniPAR PAR (over LED)", ylabel="Ambit LED setting")
        print("[LED cal] uploading old values due to poor fit quality")
        return cal

    print(f"[LED cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current act_led_coeff={old:.4f})")
    if upload:
        helpers.set_ambit_led_gain(port_ambit, slope)
        new = helpers.ambit_reboot(port_ambit).act_led_coeff
        print(f"[LED cal] uploaded LED gain: {old:.4f} -> {new:.4f}")
    return cal


def main():

    # 0. Flash the Ambit with the latest published firmware release
    if FORCE_FLASH_FIRMWARE:
        print("WARNING: FORCE_FLASH_FIRMWARE is True - the device will be re-flashed even if it already runs the latest firmware")
        print("=== Flashing firmware ===")
        rc = flash_firmware(force_flash=True)
        if rc != 0:
            raise SystemExit(f"Firmware flashing failed (exit code {rc})")
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

    # 5. Flash the latest release if the device is behind it. The version was
    #    already read from the boot dump above, so pass it in instead of
    #    re-probing the serial port. force_flash stays off here even when
    #    FORCE_FLASH_FIRMWARE is set: step 0 has already done that flash and
    #    forcing again would write the same images twice.
    print("\n=== Firmware check ===")
    current_fw = info_precalibration.FW.decode(errors="replace").strip() or None
    rc = flash_firmware(force_flash=False, current_version=current_fw)
    if rc != 0:
        # A responding Ambit can still be calibrated, so an unreachable release
        # (offline bench, no release published yet) must not kill the session -
        # unlike the explicit FORCE_FLASH_FIRMWARE run above, which is *about*
        # flashing and does abort.
        print("[flash] continuing the calibration with the firmware already on the device")
    else:
        time.sleep(1.0)            # let the device finish rebooting
        helpers._invalidate_port_cache()   # COM topology may have changed

    # 6. Rename ambit
    if RENAME_AMBIT:
        current_name = info_precalibration.name.decode(errors="replace").strip()
        new_name = input(f"Enter new name for Ambit (current: {current_name}): ").strip()
        helpers.set_ambit_name(port_ambit, new_name)

    # 7. PAR-sensor calibration (needs the DC source + PAR-reference MiniPAR)
    par_cal = None
    if port_ref and port_dc:
        print("\n=== PAR sensor calibration ===")
        par_cal = calibrate_par_sensor(port_ambit, port_ref, port_dc)
    else:
        missing = ", ".join(n for n, p in (("Par_REF MiniPAR", port_ref),
                                           ("Kiprim DC source", port_dc)) if p is None)
        print(f"\n[skip] PAR sensor calibration - missing: {missing}")

    # 8. Actinic-LED calibration (needs the Emit_LED MiniPAR)
    led_cal = None
    if port_emit:
        print("\n=== Actinic LED calibration ===")
        led_cal = calibrate_led(port_ambit, port_emit)
    else:
        print("\n[skip] Actinic LED calibration - missing: Emit_LED MiniPAR")

    # 9. Final state
    print("\n=== Ambit after calibration ===")
    info_postcalibration = helpers.ambit_reboot(port_ambit)
    print(info_postcalibration)

    # 10. Build the calibration payload (aborts with a warning if either dump is empty)
    print("\n=== Calibration payload ===")
    payload = helpers.make_calibration_payload(
        info_precalibration, info_postcalibration,
        par_cal=par_cal, led_cal=led_cal,
    )
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
