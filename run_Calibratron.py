"""Calibratron runner.

One pass over one Ambit: flash the latest released firmware, name it, calibrate
the PAR sensor and the actinic LED against the bench references, store the
result locally and publish it to openJII.

Designed to be run back to back while Ambits are swapped and the rest of the
bench stays plugged in, so it asks the operator exactly one question - the name
printed on the device - and does everything else unattended. Discovered COM
ports are remembered in ``.port_roles.json`` and firmware releases are cached
under ``firmware_ambit/releases/``, so the second device onwards skips both the
bus scan and the download.

The calibration steps need extra hardware connected:
  - a Kiprim DC source              -> answers "KIPRIM"  to "*IDN?"
  - a MiniPAR used as PAR reference -> answers "Par_REF"  to "get_name"
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
            name = re.split(r"[<>=!~ \[]", line, maxsplit=1)[0].strip().lower()
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
CALIBRATIONS_DIR = os.path.join(HERE, "calibrations")   # where save_payload() writes

PAR_CAL_CURRENTS = [0.8, 2.4, 3.0, 4.0, 6.6, 0.0]   # A, DC source -> calibration lamp
LED_CAL_SETTINGS = [10, 20, 60, 90, 150, 250, 0]    # Ambit actinic LED steps
ARRUN_PULSES     = 5        # ADPD pulses recorded per light level
ARRUN_FREQ_HZ    = 10       # ADPD pulse rate within one arrun
UPLOAD_GAINS     = True     # False -> preview the fit without writing to the device
RENAME_AMBIT     = True     # False -> keep the name already on the device

# Firmware: always track the latest ambit-iot GitHub release. Pin a tag (e.g.
# "v1.1.0") to reproduce an older calibration; set FORCE_FLASH to re-flash a
# device that already reports the target version.
AMBIT_FW_TAG     = None
FORCE_FLASH      = False

# Fit quality: below this, the fit is treated as suspect - the plot is shown and
# the gain is not uploaded.
MIN_R2 = 0.99

# openJII ingest. Topic layout is
# experiment/data_ingest/v1/<experiment>/<family>/<version>/<sensor>/<protocol>
# (open-jii/apps/tools/multispeq_mqtt_interface/.../config.py). The AWS IoT rule
# prepends topic() and clientid() to the payload, so neither is sent here.
PUBLISH_TO_OPENJII = True
OJII_EXPERIMENT_ID = "993ae58e-2e87-45ef-96e1-5bbdb0916817"
OJII_SENSOR_FAMILY = "ambit"
OJII_SENSOR_VERSION = "v1.0"
OJII_SENSOR_ID     = "ambit_calibration_1"   # must match the X.509 Thing name
OJII_PROTOCOL_ID   = "CALIBRATION"
OJII_CERTS_DIR     = "ambit_calibration_1_certs/ambit_calibration_1_certs"
OJII_ENDPOINT      = "a3qrmjf5m5y241-ats.iot.eu-central-1.amazonaws.com"

# Handshakes that identify each instrument on the bus.
DEVICE_SPECS = {
    "ambit":    ("hello\n",    "NEW"),
    "par_ref":  ("get_name\n", "Par_REF"),
    "emit_led": ("get_name\n", "Emit_LED"),
    "dc":       ("*IDN?\n",    "KIPRIM"),
}


# ============================================================================
# Firmware
# ============================================================================

def _detect_ambit_version(port=None):
    """Return the firmware version string the Ambit reports, or None.

    :param port: known Ambit port; when None the Ambit is re-discovered
        (cache-first, so the port it was just flashed on is tried first)
    """
    if port is None:
        helpers._invalidate_port_cache()   # COM topology may have changed
        port = helpers.discover_roles({"ambit": DEVICE_SPECS["ambit"]})["ambit"]
        if port is None:
            return None
    fw = helpers.ambit_reboot(port).FW
    return fw.decode(errors="replace").strip() if fw else None


def flash_latest_firmware(current_version=None, tag=AMBIT_FW_TAG, force=FORCE_FLASH):
    """Flash the Ambit with a firmware release from GitHub.

    Resolves the release (latest unless ``tag`` is pinned), skips the flash when
    the device already reports that version, then verifies what came up.
    Downloads are cached and checksum-verified by
    :func:`helpers.fetch_ambit_release`, so repeat runs do not re-download.

    Flashing does not touch the NVS partition, so the device name and the
    existing calibration coefficients survive.

    :param current_version: version the device reports now, or None if unknown
    :param tag: release tag to flash; None means the latest release
    :param force: flash even when the device already runs the target version
    :return: ``(release, flashed)`` - the release dict from
        :func:`helpers.fetch_ambit_release` and whether esptool ran
    :raises RuntimeError: if the release cannot be fetched or the flash fails
    """
    release = helpers.fetch_ambit_release(tag=tag)
    target = release["version"]

    if not force and current_version == target:
        print(f"[flash] Ambit already runs firmware {target} - skipping flash")
        return release, False
    if force:
        print(f"[flash] FORCE_FLASH set - flashing {target} regardless of current version")
    else:
        print(f"[flash] Ambit runs {current_version!r}, release is {target!r} - flashing")

    helpers.flash_ambit_firmware(firmware_dir=release["dir"],
                                 layout=release["layout"],
                                 chip=release["chip"],
                                 force_flash=True)
    time.sleep(1.0)                     # let the device finish rebooting
    helpers._invalidate_port_cache()    # the flasher re-enumerates the port

    running = _detect_ambit_version()
    if running != target:
        warnings.warn(f"Firmware flashed is NOT the one expected "
                      f"(running {running!r}, expected {target!r})")
    else:
        print(f"[flash] verified firmware {running}")
    return release, True


# ============================================================================
# Output
# ============================================================================

def save_payload(payload, mac=None, directory=CALIBRATIONS_DIR):
    """Write the calibration payload to '<YYYY-MM-DD_HH-MM-SS>_<MAC>.json'.

    :param payload: the JSON string (or dict) from helpers.make_calibration_payload
    :param mac: device MAC for the filename; if None, read from payload["device_id"]
    :param directory: target folder (created if missing); defaults to ./calibrations
    :return: the path of the file written
    """
    data = json.loads(payload) if isinstance(payload, str) else payload
    mac  = mac or data.get("device_id") or "UNKNOWN"
    # Stored indented, and with `sample` expanded, so a saved calibration stays
    # readable; what goes on the wire stays the compact openJII form.
    readable = dict(data)
    if isinstance(readable.get("sample"), str):
        try:
            readable["sample"] = json.loads(readable["sample"])
        except ValueError:
            pass
    fname = f"{datetime.now():%Y-%m-%d_%H-%M-%S}_{mac}.json"
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(readable, f, indent=2)
    print(f"[save] wrote {path}")
    return path


def _print_arrun_table(records, level_label, levels):
    """Print one row per light level with the ADPD channel statistics.

    ``leaf`` is listed first: it is the channel the operator watches, and its
    spread across the pulses at a fixed light level is the quickest read on
    whether the detector is behaving.

    :param records: per-level arrun records (None where the run failed)
    :param level_label: header for the light-level column
    :param levels: the light levels, aligned with ``records``
    """
    channels = ("leaf", "sun", "s_630", "r_630", "env")
    header = f"{level_label:>10} | " + " | ".join(f"{c:>17}" for c in channels)
    print(header)
    print("-" * len(header))
    for level, record in zip(levels, records):
        stats = helpers.summarize_arrun(record)
        cells = []
        for channel in channels:
            s = stats.get(channel)
            cells.append(f"{s['mean']:9.1f}+-{s['std']:<6.1f}" if s else f"{'-':^17}")
        print(f"{level:>10} | " + " | ".join(cells))
    print(f"({ARRUN_PULSES} pulses per level at {ARRUN_FREQ_HZ} Hz; mean +- sd)")


def _fit(x, y):
    """Least-squares straight line through (x, y).

    :return: ``(coeffs, slope, intercept, r2)``
    """
    coeffs = np.polyfit(x, y, 1)
    r2 = float(helpers.r_squared(y, np.polyval(coeffs, x)))
    return coeffs, float(coeffs[0]), float(coeffs[1]), r2


def _maybe_plot(label, x, y, coeffs, r2, xlabel, ylabel):
    """Show the fit only when it needs looking at.

    A calibration that fits to R^2 >= MIN_R2 tells the operator nothing they
    can't read off the printed slope, and plt.show() blocks the run until the
    window is closed. Good fits are silent; poor ones open the plot.

    :return: True if the plot was shown
    """
    if r2 >= MIN_R2:
        print(f"[{label}] R^2={r2:.6f} >= {MIN_R2} - plot skipped")
        return False
    helpers.plot_data_and_fit(x, y, coeffs, r2, xlabel=xlabel, ylabel=ylabel,
                              title=f"{label}  (R^2 = {r2:.6f})", show=True)
    return True


# ============================================================================
# Calibration steps
# ============================================================================

def calibrate_par_sensor(port_ambit, port_ref, port_dc, currents=PAR_CAL_CURRENTS,
                         upload=UPLOAD_GAINS, reference=None, current_slope=None):
    """Sweep the calibration lamp, fit Ambit-raw PAR against the MiniPAR
    reference, and (optionally) upload the slope as the Ambit PAR gain.

    Also records, per lamp current, what the MiniPAR PAR would be if it were
    computed from the *Ambit's* channels using the MiniPAR's own definition.
    The two firmwares weight the spectrum differently
    (:data:`helpers.AMBIT_PAR_DISCREPANCIES`), so this quantifies how much of
    the residual is spectral mismatch rather than a missing scale factor.

    :param reference: MiniPAR settings from :func:`helpers.read_minipar_reference`,
        used for the recomputation; None skips it
    :param current_slope: the Ambit's light_slope before this run, if already
        known; passing it avoids an extra reboot to read it back
    :return: a JSON-ready dict with the sweep, both devices' raw channels, the
        fitted slope / R^2 and the cross-method comparison
    """
    coefficients = (reference or {}).get("par_coefficients")

    ref_par, ref_par_raw, ref_spec = [], [], []
    ambit_par, ambit_spec, ambit_wrapped, ambit_par_mp = [], [], [], []
    arrun = []

    for I in currents:
        helpers.set_current(port=port_dc, current=I)
        time.sleep(1.0)
        ref_par.append(helpers.get_par_MP(port_ref))
        ref_par_raw.append(helpers.get_par_raw_MP(port_ref))
        ref_spec.append(helpers.get_spec_raw_MP(port_ref))
        par, spec = helpers.get_par_AMB(port_ambit, raw=True, return_spec=True)
        ambit_par.append(par)
        ambit_spec.append(spec)
        _raw, wrapped = helpers.ambit_spec_unscale(spec)
        ambit_wrapped.append(bool(wrapped and any(w is True for w in wrapped)))
        ambit_par_mp.append(helpers.ambit_par_minipar_method(spec, coefficients))
        # ADPD trace of the lamp at this intensity: pulse LEDs zeroed, actinic off
        arrun.append(helpers.record_arrun_AMB(port_ambit, actinic=0,
                                              num_points=ARRUN_PULSES,
                                              freq=ARRUN_FREQ_HZ))
    helpers.set_current(port=port_dc, current=0.0)

    x, y = np.array(ambit_par), np.array(ref_par)
    coeffs, slope, intercept, r2 = _fit(x, y)

    if any(ambit_wrapped):
        levels = [c for c, w in zip(currents, ambit_wrapped) if w]
        warnings.warn(f"Ambit spectrometer channels overflowed uint16 at lamp "
                      f"currents {levels} A - the raw PAR at those points is wrong. "
                      f"Lower PAR_CAL_CURRENTS or fix Spec_COE scaling in firmware.")

    cal = {
        "currents_A":     list(currents),
        "ambit_par_raw":  [float(v) for v in ambit_par],
        "ambit_spec":     ambit_spec,
        "ambit_spec_overflow": ambit_wrapped,
        "ref_par":        [float(v) for v in ref_par],
        "ref_par_raw":    ref_par_raw,
        "ref_spec":       ref_spec,
        "fit": {"slope": slope, "intercept": intercept, "r2": r2,
                "x": "ambit_par_raw", "y": "ref_par"},
        "arrun": arrun,
    }

    # Cross-check the MiniPAR method as implemented here against what the MiniPAR
    # itself reported. A mismatch means the port in helpers is wrong, so the
    # comparison below cannot be trusted.
    status = (reference or {}).get("spec_status") or {}
    if coefficients and all(status.get(k) is not None for k in ("gain", "atime", "astep")):
        divisor = helpers.basic_count_divisor(status["gain"], status["atime"], status["astep"])
        recomputed = [helpers.par_from_counts((s or {}).get("counts"), coefficients, divisor)
                      for s in ref_spec]
        cal["ref_par_raw_recomputed"] = recomputed
        pairs = [(a, b) for a, b in zip(ref_par_raw, recomputed)
                 if a and b and abs(a) > 1e-9]
        worst = max((abs(b - a) / abs(a) for a, b in pairs), default=None)
        cal["ref_method_check_max_rel_error"] = worst
        if worst is not None and worst > 0.01:
            warnings.warn(f"Recomputed MiniPAR PAR differs from the device's own by "
                          f"up to {worst:.1%} - treat par_minipar_method as unverified")

    if any(v is not None for v in ambit_par_mp):
        cal["ambit_par_minipar_method"] = ambit_par_mp
        cal["par_method_discrepancies"] = helpers.AMBIT_PAR_DISCREPANCIES
        usable = [(m, r) for m, r in zip(ambit_par_mp, ref_par) if m]
        if len(usable) >= 2:
            mx = np.array([m for m, _ in usable])
            my = np.array([r for _, r in usable])
            _c, mp_slope, _i, mp_r2 = _fit(mx, my)
            cal["fit_minipar_method"] = {"slope": mp_slope, "r2": mp_r2,
                                         "x": "ambit_par_minipar_method", "y": "ref_par"}
            print(f"[PAR cal] MiniPAR-method PAR from Ambit channels: "
                  f"slope={mp_slope:.4f} R^2={mp_r2:.6f}  "
                  f"(firmware PAR: slope={slope:.4f} R^2={r2:.6f})")
            if mp_r2 > r2 + 0.005:
                print("[PAR cal] the MiniPAR weighting linearises this sweep better than "
                      "the firmware's - see par_method_discrepancies in the payload")

    old = (current_slope if current_slope is not None
           else helpers.ambit_reboot(port_ambit).light_slope)
    print(f"[PAR cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current light_slope={old:.4f})")

    print("\n[PAR cal] ADPD pulses per lamp current (actinic off):")
    _print_arrun_table(arrun, "lamp A", currents)

    _maybe_plot("PAR cal", x, y, coeffs, r2,
                "Ambit PAR (raw)", "MiniPAR PAR (reference)")

    cal["light_slope_before"] = float(old)
    if r2 < MIN_R2:
        print(f"[PAR cal] R^2 below {MIN_R2} - keeping the existing PAR gain")
        cal["uploaded"] = False
        return cal

    if upload:
        helpers.set_par_gain(port_ambit, slope)
        new = helpers.ambit_reboot(port_ambit).light_slope
        print(f"[PAR cal] uploaded PAR gain: {old:.4f} -> {new:.4f}")
        cal["light_slope_after"] = float(new)
    cal["uploaded"] = bool(upload)
    return cal


def calibrate_led(port_ambit, port_emit, settings=LED_CAL_SETTINGS, upload=UPLOAD_GAINS,
                  current_coeff=None):
    """Sweep the Ambit actinic LED, fit measured PAR against LED setting, and
    (optionally) upload the slope as the Ambit LED gain.

    At every LED setting an ADPD run records ARRUN_PULSES pulses, and their
    per-channel statistics - ``leaf`` above all - are printed and stored, so the
    detector response across the actinic range is part of the calibration record.

    :param current_coeff: the Ambit's act_led_coeff before this run, if already
        known; passing it avoids an extra reboot to read it back
    :return: a JSON-ready dict with the sweep, the reference MiniPAR's raw
        channels, the ADPD pulses per setting, and the fitted slope / R^2
    """
    led_setting, measured, ref_spec, arrun = [], [], [], []
    for s in settings:
        helpers.set_ambit_led(port_ambit, s)
        time.sleep(0.2)
        measured.append(helpers.get_par_MP(port_emit))
        ref_spec.append(helpers.get_spec_raw_MP(port_emit))
        # ADPD trace with the actinic LED driven at this setting; must come after
        # the MiniPAR reads (opening the Ambit port resets the latched LED).
        # Note the firmware forces the actinic off for settings <= 3.
        arrun.append(helpers.record_arrun_AMB(port_ambit, actinic=s,
                                              num_points=ARRUN_PULSES,
                                              freq=ARRUN_FREQ_HZ))
        led_setting.append(s)

    x, y = np.array(measured), np.array(led_setting)
    coeffs, slope, intercept, r2 = _fit(x, y)

    cal = {
        "led_settings":   list(settings),
        "ref_par":        [float(v) for v in measured],
        "ref_spec":       ref_spec,
        "fit": {"slope": slope, "intercept": intercept, "r2": r2,
                "x": "ref_par", "y": "led_settings"},
        "arrun": arrun,
        "arrun_stats": [helpers.summarize_arrun(a) for a in arrun],
    }

    old = (current_coeff if current_coeff is not None
           else helpers.ambit_reboot(port_ambit).act_led_coeff)
    print(f"[LED cal] fit slope={slope:.4f}  R^2={r2:.6f}  (current act_led_coeff={old:.4f})")

    print("\n[LED cal] ADPD pulses per actinic setting:")
    _print_arrun_table(arrun, "actinic", settings)

    _maybe_plot("LED cal", x, y, coeffs, r2,
                "MiniPAR PAR (over LED)", "Ambit LED setting")

    cal["act_led_coeff_before"] = float(old)
    if r2 < MIN_R2:
        print(f"[LED cal] R^2 below {MIN_R2} - keeping the existing LED gain "
              f"(check the plot for outliers or nonlinearity)")
        cal["uploaded"] = False
        return cal

    if upload:
        helpers.set_ambit_led_gain(port_ambit, slope)
        new = helpers.ambit_reboot(port_ambit).act_led_coeff
        print(f"[LED cal] uploaded LED gain: {old:.4f} -> {new:.4f}")
        cal["act_led_coeff_after"] = float(new)
    cal["uploaded"] = bool(upload)
    return cal


# ============================================================================
# Runner
# ============================================================================

def main():
    # 1. Find everything on the bus in one pass, cached ports first.
    t0 = time.perf_counter()
    ports = helpers.discover_roles(DEVICE_SPECS)
    print(f"[discover] {time.perf_counter() - t0:.2f}s  "
          + "  ".join(f"{r}={p or '-'}" for r, p in ports.items()))

    port_ambit = ports["ambit"]
    if port_ambit is None:
        raise SystemExit("No Ambit device found on any serial port")
    port_ref, port_emit, port_dc = ports["par_ref"], ports["emit_led"], ports["dc"]

    # 2. Read the as-received state, then ask the operator for the name. Doing
    #    this before the flash gets the one manual step out of the way early.
    info_asreceived = helpers.ambit_reboot(port_ambit)
    fw_asreceived = info_asreceived.FW.decode(errors="replace").strip()
    current_name = info_asreceived.name.decode(errors="replace").strip()
    print(info_asreceived)

    new_name = None
    if RENAME_AMBIT:
        new_name = input(f"\nEnter new name for Ambit (current: {current_name}, "
                         f"blank to keep): ").strip() or None

    # 3. Firmware from the latest ambit-iot GitHub release.
    print("\n=== Firmware ===")
    release, flashed = flash_latest_firmware(current_version=fw_asreceived)
    if flashed:
        # Re-read: the flash changes the reported version, and the bridge may
        # have re-enumerated onto a different COM port.
        ports = helpers.discover_roles(DEVICE_SPECS)
        port_ambit = ports["ambit"] or port_ambit
        port_ref, port_emit, port_dc = ports["par_ref"], ports["emit_led"], ports["dc"]
        info_precalibration = helpers.ambit_reboot(port_ambit)
    else:
        # Nothing has touched the device since it was read, so reuse that dump
        # rather than paying for another reboot.
        info_precalibration = info_asreceived

    # 4. Name the device. This happens after the pre-calibration snapshot so the
    #    payload's device_before diff records what the device was called before.
    if new_name:
        helpers.set_ambit_name(port_ambit, new_name)
        print(f"[name] {current_name!r} -> {new_name!r}")

    # 5. Snapshot the reference MiniPAR's settings once; they define the PAR the
    #    Ambit is being fitted against.
    reference = helpers.read_minipar_reference(port_ref) if port_ref else None

    # 6. PAR-sensor calibration (needs the DC source + PAR-reference MiniPAR)
    par_cal = None
    if port_ref and port_dc:
        print("\n=== PAR sensor calibration ===")
        par_cal = calibrate_par_sensor(port_ambit, port_ref, port_dc, reference=reference,
                                       current_slope=info_precalibration.light_slope)
    else:
        missing = ", ".join(n for n, p in (("Par_REF MiniPAR", port_ref),
                                           ("Kiprim DC source", port_dc)) if p is None)
        print(f"\n[skip] PAR sensor calibration - missing: {missing}")

    # 7. Actinic-LED calibration (needs the Emit_LED MiniPAR)
    led_cal = None
    if port_emit:
        print("\n=== Actinic LED calibration ===")
        led_cal = calibrate_led(port_ambit, port_emit,
                                current_coeff=info_precalibration.act_led_coeff)
    else:
        print("\n[skip] Actinic LED calibration - missing: Emit_LED MiniPAR")

    # 8. Final state
    print("\n=== Ambit after calibration ===")
    info_postcalibration = helpers.ambit_reboot(port_ambit)
    print(info_postcalibration)

    # 9. Build the payload (aborts with a warning if either dump is empty)
    payload = helpers.make_calibration_payload(
        info_precalibration, info_postcalibration,
        par_cal=par_cal, led_cal=led_cal,
        protocol_id=OJII_PROTOCOL_ID,
        station={
            "firmware_release":      release["tag"],
            "firmware_flashed":      flashed,
            "firmware_as_received":  fw_asreceived,
            "par_reference":         reference,
            "ambit_spec_channels":   helpers.AMBIT_SPEC_CHANNELS,
            "ref_spec_channels":     helpers.MINIPAR_AS7341_CHANNELS,
            "arrun":                 {"pulses": ARRUN_PULSES, "freq_hz": ARRUN_FREQ_HZ,
                                      "channels": list(helpers.ARRUN_CHANNELS)},
        },
    )

    # 10. Save locally, then publish to openJII.
    print("\n=== Saving payload ===")
    path = save_payload(payload, mac=info_postcalibration.MAC)

    if not PUBLISH_TO_OPENJII:
        print("[publish] PUBLISH_TO_OPENJII is False - not uploading")
        return payload

    topic = (f"experiment/data_ingest/v1/{OJII_EXPERIMENT_ID}/{OJII_SENSOR_FAMILY}/"
             f"{OJII_SENSOR_VERSION}/{OJII_SENSOR_ID}/{OJII_PROTOCOL_ID}")
    try:
        helpers.publish_payload_mqtt5(payload, topic=topic,
                                      certs_dir=OJII_CERTS_DIR,
                                      endpoint=OJII_ENDPOINT)
        print("[publish] uploaded to openJII")
    except Exception as exc:
        # The calibration is already on disk, so a network problem must not
        # look like a failed calibration.
        warnings.warn(f"openJII upload failed ({exc}); the calibration is saved at {path}")
    return payload


if __name__ == "__main__":
    main()
