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
    if getattr(sys, "frozen", False):
        return
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


if __name__ == "__main__":
    _ensure_requirements()  # source CLI only; importing the GUI must not install packages

import numpy as np
import helpers; importlib.reload(helpers)
import firmware_fetch; importlib.reload(firmware_fetch)
import calibration_quality; importlib.reload(calibration_quality)

# ---- paths / tunables -----------------------------------------------------
# Anchor to the directory that contains helpers.py (== this script's folder).
# Using helpers.__file__ is robust in notebooks where this module's own
# __file__ may be a relative path and the kernel CWD is the workspace root,
# which would otherwise make os.path.abspath(__file__) point to the wrong dir.
from runtime_paths import data_dir

HERE             = str(data_dir())
# Downloaded Ambit firmware releases land here as firmware_cache/<version>/
# (manifest.json + images); git-ignored, populated by firmware_fetch.
FIRMWARE_CACHE_DIR = os.path.join(HERE, "firmware_cache")
CALIBRATIONS_DIR = os.path.join(HERE, "calibrations")   # where save_payload() writes

PAR_CAL_CURRENTS = [0.8, 2.4, 3.0, 4.0, 6.6, 0.0]   # A, DC source -> calibration lamp
# PAR_CAL_CURRENTS = [0.2, 0.4, 0.8, 1.0, 1.6, 0.0]   # A, DC source -> calibration lamp
LED_CAL_SETTINGS = [10, 20, 60, 90, 150, 250, 0]          # Ambit actinic LED steps
UPLOAD_GAINS     = True   # set False to preview the fit/plot without writing to the device
FORCE_FLASH_FIRMWARE   = False     # True -> reflash or recover an unresponsive device
ALLOW_FIRMWARE_DOWNGRADE = False   # separate explicit override; normally never enable
RENAME_AMBIT = True
CALIBRATE_ADPD_BASELINE = True

# Release selection is controlled by firmware_fetch: approved v1.1.3-rc1 until
# an equal/newer immutable stable release exists. Arbitrary prereleases are not
# selected (see firmware_fetch.fetch_latest).

# Ambit firmware >= 0.1.0 answers `hello` with "NEW <name> Ready FW:<version>",
# so the version can be read without the (slower, reboot-triggering) boot dump.
_HELLO_FW_RE = re.compile(
    r"FW:\s*([0-9]+(?:\.[0-9]+){2}(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?)"
)
_firmware_release_provenance = None


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
        reply = helpers._ambit_query(port, helpers.AmbitProto.HELLO, timeout=2.0)
    except Exception as exc:                      # serial hiccup: try the boot dump
        print(f"[flash] could not read the hello reply on {port}: {exc}")
        reply = ""
    match = _HELLO_FW_RE.search(reply or "")
    if match:
        return match.group(1).strip()

    # Old firmware: no version in the hello reply, only in the boot dump.
    fw = helpers.ambit_reboot(port).FW
    return fw.decode(errors="replace").strip() if fw else None


def flash_firmware(force_flash=False, current_version=None,
                   allow_downgrade=False, cache_root=FIRMWARE_CACHE_DIR):
    """Fetch the approved Ambit firmware and flash only when safely authorized.

    The images come from a policy-selected immutable public AMBIT GitHub release,
    downloaded into ``cache_root/<version>/`` by firmware_fetch (which may fall
    back to a verified cache entry when offline). helpers.flash_ambit_firmware()
    locates the flasher COM port, opens it for esptool, and closes it afterwards.

    Flashing is decided as follows:
      - target numeric version newer than device -> upgrade.
      - numeric versions equivalent -> skip (unless ``force_flash=True``).
      - target older than device -> skip unless ``allow_downgrade=True``.
      - current version unknown -> skip normally; ``force_flash=True`` recovers.

    AMBIT reports only the numeric core, so device ``1.1.3`` is equivalent to
    release ``1.1.3-rc1`` and is not repeatedly reflashed.

    :param force_flash: re-flash even when the device is already up to date.
    :param current_version: firmware version already read from the device; when
        None it is detected over serial.
    :param allow_downgrade: explicit permission to install an older target;
        separate from force-reflash so force alone cannot downgrade.
    :param cache_root: firmware cache folder to download into.
    :return: 0 on success (including a deliberately skipped flash), 1 on failure.
    """
    global _firmware_release_provenance
    try:
        version, firmware_dir = firmware_fetch.fetch_latest(cache_root)
        _firmware_release_provenance = firmware_fetch.release_provenance(firmware_dir)
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"[flash] could not obtain the Ambit firmware: {exc}")
        return 1
    print(f"[flash] selected immutable firmware: {version} ({firmware_dir})")

    current = current_version or _detect_ambit_version()
    decision = firmware_fetch.flash_decision(
        current, version, force=force_flash, allow_downgrade=allow_downgrade,
    )
    if decision == "equivalent":
        visible = firmware_fetch.device_visible_version(version)
        print(f"[flash] Ambit firmware {current!r} is device-equivalent to "
              f"release {version!r} ({visible}) - skipping flash")
        return 0
    if decision == "newer":
        print(f"[flash] Ambit firmware {current!r} is newer than target {version!r} "
              "- refusing automatic downgrade")
        return 0
    if decision == "unknown":
        print(f"[flash] cannot prove firmware {version!r} is an upgrade because the "
              f"current version is {current!r} - skipping. Recovery flashing "
              "requires force_flash=True")
        return 0
    if decision == "downgrade":
        print(f"[flash] WARNING: explicit downgrade authorized: {current!r} -> {version!r}")
    elif decision == "recovery":
        print(f"[flash] WARNING: forced recovery authorized with unknown current "
              f"version; flashing {version!r}")
    elif decision == "reflash":
        print(f"[flash] force-reflashing device-equivalent firmware {current!r}")
    else:
        print(f"[flash] upgrading Ambit firmware {current!r} -> {version!r}")

    try:
        flashed = helpers.flash_ambit_firmware(firmware_dir=firmware_dir,
                                               force_flash=True)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[flash] flashing failed: {exc}")
        return 1
    print(f"[flash] {'firmware flashed' if flashed else 'flash skipped'}")

    # Verify the freshly-flashed firmware matches the release we just fetched.
    if flashed:
        time.sleep(1.0)   # let the device finish rebooting
        running = _detect_ambit_version()
        try:
            verified = firmware_fetch.compare_device_versions(running, version) == 0
        except (TypeError, ValueError):
            verified = False
        if not verified:
            warnings.warn(f"Firmware flashed NOT the one expected "
                          f"(running {running!r}, expected device-visible "
                          f"{firmware_fetch.device_visible_version(version)!r} "
                          f"from release {version!r})")
        else:
            print(f"[flash] verified device-equivalent firmware {running}")

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
    safe_mac = re.sub(r"[^A-Za-z0-9_.-]", "_", str(mac))
    fname = f"{datetime.now():%Y-%m-%d_%H-%M-%S_%f}_{safe_mac}.json"
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[save] wrote {path}")
    return path


def _write_gain_with_readback(port, *, target, previous, setter, info_field, label):
    """Write a gain, verify it after reboot, and restore the old value on failure."""
    wire_target = round(float(target), 4)
    setter(port, wire_target)
    observed = float(getattr(helpers.ambit_reboot(port), info_field))
    if np.isclose(observed, wire_target, rtol=1e-4, atol=5e-5):
        print(f"[{label}] verified persisted gain: {previous:.4f} -> {observed:.4f}")
        return observed

    restore_error = None
    try:
        setter(port, previous)
        restored = float(getattr(helpers.ambit_reboot(port), info_field))
        if not np.isclose(restored, previous, rtol=1e-4, atol=5e-5):
            restore_error = f"restore readback was {restored:.6g}, expected {previous:.6g}"
    except Exception as exc:  # preserve the original verification context
        restore_error = str(exc)
    detail = f"; previous value restoration failed: {restore_error}" if restore_error else "; previous value restored"
    raise RuntimeError(
        f"{label} calibration write was not verified (read {observed:.6g}, "
        f"expected {wire_target:.6g}){detail}"
    )


def calibrate_adpd_baseline(port_ambit, previous, *, upload=UPLOAD_GAINS, input_fn=input):
    """Measure a dark baseline and atomically persist all six channels after QC."""
    confirmation = input_fn(
        "Install the dark fixture, block ambient light, then type DARK to measure "
        "the six-channel ADPD baseline (anything else skips): "
    ).strip()
    if confirmation != "DARK":
        print("[ADPD baseline] skipped; no calibration value was changed")
        return {"status": "skipped", "reason": "dark fixture not confirmed"}

    measured = helpers.measure_adpd_baseline(port_ambit)
    reasons = []
    if len(previous) != 6 or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 0xFFFFFF
        for value in previous
    ):
        reasons.append("existing six-channel baseline could not be read; rollback is not safe")
    if len(measured) != 6 or any(value < 0 or value > 0xFFFFFF for value in measured):
        reasons.append("baseline must contain six unsigned 24-bit values")
    if measured and measured[0] > 400:
        reasons.append("s_630 dark baseline exceeds the firmware safety limit of 400")
    result = {
        "status": "passed" if not reasons else "rejected",
        "measured": measured,
        "previous": list(previous),
        "quality": {"passed": not reasons, "reasons": reasons, "s_630_max": 400},
        "uploaded": False,
    }
    if reasons:
        print("[ADPD baseline] rejected: " + "; ".join(reasons))
        return result
    if not upload:
        print(f"[ADPD baseline] preview only: {measured}")
        return result

    helpers.set_adpd_baseline(port_ambit, measured)
    observed = list(helpers.ambit_reboot(port_ambit).adpd_calibration)
    if observed != measured:
        restore_error = None
        try:
            helpers.set_adpd_baseline(port_ambit, list(previous))
            restored = list(helpers.ambit_reboot(port_ambit).adpd_calibration)
            if restored != list(previous):
                restore_error = f"restore readback was {restored!r}"
        except Exception as exc:
            restore_error = str(exc)
        detail = f"; previous baseline restoration failed: {restore_error}" if restore_error else "; previous baseline restored"
        raise RuntimeError(
            f"ADPD baseline write was not verified (read {observed!r}, expected {measured!r}){detail}"
        )
    result["uploaded"] = True
    result["readback"] = observed
    print(f"[ADPD baseline] saved and verified: {observed}")
    return result


def calibrate_par_sensor(port_ambit, port_ref, port_dc, currents=PAR_CAL_CURRENTS, upload=UPLOAD_GAINS, *, show_plot=True):
    """Sweep the calibration lamp, fit Ambit-raw PAR vs MiniPAR reference, show
    the plot, and (optionally) upload the slope as the Ambit PAR gain.

    :return: a JSON-ready dict with the sweep (x/y arrays + axis labels), the
        raw spectrometer channels of both devices, the fitted slope and r2.
    """
    ref_par, ambit_raw = [], []
    ref_spec, ambit_spec, arrun = [], [], []
    for I in currents:
        helpers.set_current(port=port_dc, current=I)
        time.sleep(1.0)
        ref_par.append(helpers.get_par_MP(port_ref))
        ref_spec.append(helpers.get_spec_raw_MP(port_ref))
        par, spec = helpers.get_par_AMB(port_ambit, raw=True, return_spec=True)
        ambit_raw.append(par)
        ambit_spec.append(spec)
        # ADPD trace of the lamp at this intensity: pulse LEDs zeroed, actinic off
        arrun.append(helpers.record_arrun_AMB(port_ambit, actinic=0))
    helpers.set_current(port=port_dc, current=0.0)

    x, y = np.array(ambit_raw), np.array(ref_par)
    quality = calibration_quality.assess_origin_fit(
        x, y, currents, coefficient_min=0.05, coefficient_max=100.0
    )
    slope = float(quality["coefficient"])
    r2 = float(quality["r2"])
    coeffs = np.array([slope, 0.0])

    cal = {
        "x": x.tolist(), "x_label": "Ambit PAR (raw)",
        "y": y.tolist(), "y_label": "MiniPAR PAR (reference)",
        "slope": slope, "r2": float(r2),
        "quality": quality,
        "currents_A": list(currents),
        "ambit_spec": ambit_spec,
        "ambit_spec_channels": ["F1_415", "F2_445", "F3_480", "F4_515", "F5_555",
                                "F6_590", "F7_630", "F8_680", "NIR", "CLEAR"],
        "ambit_spec_note": "values pre-scaled by firmware Spec_COE {12,10,11,10,10,9,7,4,1,1}, uint16 wrap",
        "ref_spec": ref_spec,
        "ref_spec_channels": ["F1_415", "F2_445", "F3_480", "F4_515", "F5_555",
                              "F6_590", "F7_630", "F8_680", "CLEAR", "NIR"],
        "arrun": arrun,
        "arrun_note": "per step: set_currents,0,0,0 then arrun2 (5 pts @ 10 Hz, ADPD pulse LEDs dark, actinic off)",
    }

    old = helpers.ambit_reboot(port_ambit).light_slope
    print(f"[PAR cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current light_slope={old:.4f})")
    if show_plot:
        helpers.plot_data_and_fit(x, y, coeffs, r2,
                                  xlabel="Ambit PAR (raw)", ylabel="MiniPAR PAR (reference)")

    if not quality["passed"]:
        print("[PAR cal] REJECTED; existing gain kept: " + "; ".join(quality["reasons"]))
        return cal
    if upload:
        _write_gain_with_readback(
            port_ambit, target=slope, previous=old, setter=helpers.set_par_gain,
            info_field="light_slope", label="PAR cal"
        )
    return cal


def calibrate_led(port_ambit, port_emit, settings=LED_CAL_SETTINGS, upload=UPLOAD_GAINS, *, show_plot=True):
    """Sweep the Ambit actinic LED, fit measured PAR vs LED setting, show the
    plot, and (optionally) upload the slope as the Ambit LED gain.

    :return: a JSON-ready dict with the sweep (x/y arrays + axis labels), the
        raw spectrometer channels of the reference MiniPAR, the fitted slope
        and r2.
    """
    led_setting, measured, ref_spec, arrun = [], [], [], []
    for s in settings:
        helpers.set_ambit_led(port_ambit, s)
        time.sleep(0.2)
        measured.append(helpers.get_par_MP(port_emit))
        ref_spec.append(helpers.get_spec_raw_MP(port_emit))
        # ADPD trace with the actinic LED driven at this setting; must come after
        # the MiniPAR reads (opening the Ambit port resets the latched LED)
        arrun.append(helpers.record_arrun_AMB(port_ambit, actinic=s))
        led_setting.append(s)

    x, y = np.array(measured), np.array(led_setting)
    quality = calibration_quality.assess_origin_fit(
        x, y, settings, coefficient_min=0.01, coefficient_max=1.0
    )
    slope = float(quality["coefficient"])
    r2 = float(quality["r2"])
    coeffs = np.array([slope, 0.0])

    cal = {
        "x": x.tolist(), "x_label": "MiniPAR PAR (over LED)",
        "y": y.tolist(), "y_label": "Ambit LED setting",
        "slope": slope, "r2": float(r2),
        "quality": quality,
        "ref_spec": ref_spec,
        "ref_spec_channels": ["F1_415", "F2_445", "F3_480", "F4_515", "F5_555",
                              "F6_590", "F7_630", "F8_680", "CLEAR", "NIR"],
        "arrun": arrun,
        "arrun_note": "per step: set_currents,0,0,0 then arrun2 (5 pts @ 10 Hz, ADPD pulse LEDs dark, actinic driven at the LED setting)",
    }

    old = helpers.ambit_reboot(port_ambit).act_led_coeff

    if not quality["passed"]:
        print("[LED cal] REJECTED; existing gain kept: " + "; ".join(quality["reasons"]))
        if show_plot:
            helpers.plot_data_and_fit(x, y, coeffs, r2,
                                    xlabel="MiniPAR PAR (over LED)", ylabel="Ambit LED setting")
        return cal

    print(f"[LED cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current act_led_coeff={old:.4f})")
    if upload:
        _write_gain_with_readback(
            port_ambit, target=slope, previous=old, setter=helpers.set_ambit_led_gain,
            info_field="act_led_coeff", label="LED cal"
        )
    return cal


def main():

    # 0. Optional explicit reflash/recovery. Known downgrade stays separately gated.
    if FORCE_FLASH_FIRMWARE:
        print("WARNING: FORCE_FLASH_FIRMWARE is True - equivalent firmware may be re-flashed or an unresponsive device recovered")
        if ALLOW_FIRMWARE_DOWNGRADE:
            print("WARNING: ALLOW_FIRMWARE_DOWNGRADE is True - an older known target may be flashed")
        print("=== Flashing firmware ===")
        rc = flash_firmware(force_flash=True,
                            allow_downgrade=ALLOW_FIRMWARE_DOWNGRADE)
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

    # 7. ADPD baseline (requires an explicitly confirmed dark fixture). The
    # firmware persists the complete vector atomically; readback is mandatory.
    baseline_cal = None
    if CALIBRATE_ADPD_BASELINE:
        print("\n=== ADPD dark baseline ===")
        current_info = helpers.ambit_reboot(port_ambit)
        baseline_cal = calibrate_adpd_baseline(
            port_ambit, current_info.adpd_calibration
        )

    # 8. PAR-sensor calibration (needs the DC source + PAR-reference MiniPAR)
    par_cal = None
    if port_ref and port_dc:
        print("\n=== PAR sensor calibration ===")
        par_cal = calibrate_par_sensor(port_ambit, port_ref, port_dc)
    else:
        missing = ", ".join(n for n, p in (("Par_REF MiniPAR", port_ref),
                                           ("Kiprim DC source", port_dc)) if p is None)
        print(f"\n[skip] PAR sensor calibration - missing: {missing}")

    # 9. Actinic-LED calibration (needs the Emit_LED MiniPAR)
    led_cal = None
    if port_emit:
        print("\n=== Actinic LED calibration ===")
        led_cal = calibrate_led(port_ambit, port_emit)
    else:
        print("\n[skip] Actinic LED calibration - missing: Emit_LED MiniPAR")

    # 10. Final state
    print("\n=== Ambit after calibration ===")
    info_postcalibration = helpers.ambit_reboot(port_ambit)
    print(info_postcalibration)

    # 11. Build the calibration payload (aborts with a warning if either dump is empty)
    print("\n=== Calibration payload ===")
    payload = helpers.make_calibration_payload(
        info_precalibration, info_postcalibration,
        par_cal=par_cal, led_cal=led_cal, baseline_cal=baseline_cal,
        firmware_release_provenance=_firmware_release_provenance,
    )
    print(payload)

    # 12. Save the payload to ./calibrations/<YYYY-MM-DD_HH-MM-SS>_<MAC>.json
    print("\n=== Saving payload ===")
    save_payload(payload, mac=info_postcalibration.MAC)
    # return payload

    # 13. (Optional) Publish the payload to AWS IoT Core via MQTT
    helpers.publish_payload_mqtt5(
        payload,
        topic="experiment/data_ingest/v1/993ae58e-2e87-45ef-96e1-5bbdb0916817/ambit/v1.0/ambit_calibration_1/1234556",
        certs_dir="ambit_calibration_1_certs/ambit_calibration_1_certs",
        endpoint="http://a3qrmjf5m5y241-ats.iot.eu-central-1.amazonaws.com",   # your AWS IoT ATS endpoint
    )


if __name__ == "__main__":
    main()
