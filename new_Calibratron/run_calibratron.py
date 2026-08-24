"""Calibratron runner - cmd-35 three-tier PAR chain.

One pass over one Ambit: flash the approved firmware, name it, calibrate the
tier-3 PAR parameters and the actinic LED against the bench references, store
the result locally and publish it to openJII.

Designed to be run back to back while Ambits are swapped and the rest of the
bench stays plugged in, so it asks the operator exactly one question - the name
printed on the device. The ADPD dark baseline is off by default: it is the only
step that needs a fixture change mid-run, and it calibrates nothing on the PAR
chain, so it does not belong in an unattended back-to-back pass. Set
``CALIBRATE_ADPD_BASELINE = True`` to bring it back.

The bench needs:
  - a Kiprim DC source              -> answers "KIPRIM"   to "*IDN?"
  - a MiniPAR as the PAR reference  -> answers "Par_REF"   to "get_name"
  - a MiniPAR over the actinic LED  -> answers "Emit_LED"  to "get_name"
Whatever is missing is reported and that step is skipped.

WHAT THIS SCRIPT CALIBRATES ON THE PAR CHAIN
--------------------------------------------
``par_slope`` and ``par_intercept`` - tier 3, two floats, per device. Nothing
else. ``spec_offset``, ``spec_sens`` and ``par_weight`` ship as firmware
defaults (plan section 7); this script reads them back to verify the firmware's
arithmetic and to record which seed generation the fit sits on, and never writes
them. A per-device tier-2 fit from a single-lamp sweep scores R^2 -31 .. -7443
(plan section 2), so the capability is deliberately absent rather than guarded.

The legacy PAR path is gone: no cmd 31, no ``get_par``, no ``set_spec``,
no ``spec_coef`` write. ``spec_coef`` is still *read* and recorded, because
cmd 31 and deployed devices depend on it (plan decision 7).
"""

from __future__ import annotations

import os
import sys
import time
import urllib.error
import warnings

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
# This folder first, the repo root appended: the root has a different module also
# called `helpers`, and it must never win.
sys.path.insert(0, _HERE)
if _ROOT not in sys.path:
    sys.path.append(_ROOT)

import helpers
import quality                  # LED origin fit + ADPD gate, firmware bounds mirrored
import spec_cal                 # cmd-35 codecs, tier math, the tier-3 affine gate

import firmware_fetch           # repo root: release selection policy


# ---- tunables --------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
CALIBRATIONS_DIR   = os.path.join(HERE, "calibrations")
FIRMWARE_CACHE_DIR = os.path.join(HERE, "firmware_cache")

#: Lamp currents for the tier-3 sweep, amps.
#:
#: The low end is load-bearing now that the intercept is fitted, but a *dark*
#: point cannot carry it: with the lamp off the channels clip at their dark
#: offsets, and a clipped reading is off-model (see
#: :func:`spec_cal.usable_for_fit`). So 0.0 A is kept as a light-tightness check
#: and 0.4 A is added as a genuine low anchor inside the linear regime - if the
#: dark point comes back unclipped it is fitted too, and if it clips the sweep
#: still has a point near the bottom.
PAR_CAL_CURRENTS = [0.0, 0.4, 0.8, 2.4, 3.0, 4.0, 6.6]

#: Currents re-read after the write to confirm the fit closed the loop.
PAR_CONFIRM_CURRENTS = [0.8, 3.0, 6.6]

LED_CAL_SETTINGS = [10, 20, 60, 90, 150, 250, 0]

#: Measure the ADPD photodiodes (leaf, sun, s_630, r_630, env) at every point of
#: both sweeps. Not a calibration - nothing is fitted or written from these - but
#: the tier-3 sweep pairs them with a calibrated PAR reference across the whole
#: lamp range, which is a PAR response curve for the leaf and sun photodiodes
#: obtained for free. The ADPD pulse LEDs are zeroed before every trace so the
#: detector sees only the incident light. Only the per-point *statistics* reach
#: the record (n, mean, sd, min, max, saturated) - the raw pulse samples are
#: reduced and discarded.
RECORD_ADPD_TRACES = True

#: Which ADPD photodiode faces the light source in each sweep. `leaf` and `sun`
#: point in different directions, so this is fixture geometry, not detector
#: health: the halogen lamp reaches `sun`, the Ambit's own actinic LED reaches
#: `leaf`. Only the named channel gets flatness / monotonicity notes; the other
#: is still recorded, and saturation is still reported for both.
TIER3_ADPD_RESPONDERS = ("sun",)
LED_ADPD_RESPONDERS   = ("leaf",)
ARRUN_PULSES  = 5      # samples per trace
ARRUN_FREQ_HZ = 10     # sampling rate within one trace -> ~0.5 s per point

LAMP_SETTLE_S = 1.0            # after changing the DC source
LED_SETTLE_S  = 0.2

#: The firmware drives the actinic only for settings > 3 (``if (actinic > 3)`` in
#: ``run_arr_type1``, ambit/src/PAM.cpp), so no response is expected at or below
#: it and a dark reading there is correct, not a failed latch.
LED_OFF_MAX_SETTING = 3

#: How many times to re-assert a latch the reference says did not take.
LED_LATCH_ATTEMPTS = 3

#: What counts as the reference seeing the LED: umol m-2 s-1 above the measured
#: dark floor, or this fraction of it, whichever is larger. Calibrated against
#: the 2026-08-18 failure, which read 5.91 dark and 217 lit - the bar only has to
#: clear the reference's own noise, not resolve the dimmest setting.
LED_MIN_RESPONSE = 2.0
LED_MIN_RESPONSE_FRACTION = 0.25

#: Refuse to run unless every bench instrument answered. A missing role used to
#: degrade quietly - "[skip] tier-3 PAR calibration - missing: Kiprim DC source"
#: still produced a saved, uploaded record - and the operator, who can see the
#: instrument sitting there powered and cabled, has to notice a line of skip text
#: to know the run was worthless. Discovery failing is a bench fault, so it stops
#: the bench. Set False only to calibrate deliberately without an instrument.
REQUIRE_ALL_DEVICES = True

UPLOAD_COEFFICIENTS = True     # False -> preview every fit, write nothing
RENAME_AMBIT        = True
CALIBRATE_TIER3     = True
CALIBRATE_LED       = True
#: Off by default. See the module docstring: it is the one step that needs the
#: operator to change the fixture, and it writes nothing on the PAR chain.
CALIBRATE_ADPD_BASELINE = False

FLASH_FIRMWARE           = True
FORCE_FLASH_FIRMWARE     = True   # True -> reflash, or recover an unresponsive device
ALLOW_FIRMWARE_DOWNGRADE = False   # separate explicit override; normally never

#: Host recompute vs firmware. A real error in the tick, the gain ordinal, the
#: per-bank divisor or the NIR/Clear swap is orders of magnitude, not ppm.
MATH_CHECK_RTOL = 2e-3

# openJII ingest. Topic layout is
# experiment/data_ingest/v1/<experiment>/<family>/<version>/<sensor>/<protocol>
# The AWS IoT rule prepends topic() and clientid(), so neither is sent here.
PUBLISH_TO_OPENJII  = True
OJII_EXPERIMENT_ID  = "993ae58e-2e87-45ef-96e1-5bbdb0916817"
OJII_SENSOR_FAMILY  = "ambit"
OJII_SENSOR_VERSION = "v1.0"
OJII_SENSOR_ID      = "ambit_calibration_1"     # must match the X.509 Thing name
OJII_PROTOCOL_ID    = "CALIBRATION"
OJII_CERTS_DIR      = os.path.join(os.path.dirname(HERE),
                                   "ambit_calibration_1_certs",
                                   "ambit_calibration_1_certs")
OJII_ENDPOINT       = "a3qrmjf5m5y241-ats.iot.eu-central-1.amazonaws.com"


# ============================================================================
# Firmware
# ============================================================================

def flash_firmware(port, *, force=False, current_version=None,
                   allow_downgrade=False):
    """Fetch the approved release and flash only when safely authorised.

    ``port`` is where the Ambit answered discovery - on this bench that is the
    flasher bridge, so no separate flasher lookup is needed.

    :return: ``(rc, provenance)`` - rc 0 on success or a deliberate skip
    """
    try:
        version, firmware_dir = firmware_fetch.fetch_latest(FIRMWARE_CACHE_DIR)
        provenance = firmware_fetch.release_provenance(firmware_dir)
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        print(f"[flash] could not obtain the Ambit firmware: {exc}")
        return 1, None
    print(f"[flash] selected immutable firmware: {version} ({firmware_dir})")

    decision = firmware_fetch.flash_decision(current_version, version, force=force,
                                             allow_downgrade=allow_downgrade)
    if decision in ("equivalent", "newer", "unknown"):
        explain = {
            "equivalent": f"device {current_version!r} is device-equivalent to "
                          f"{version!r} - skipping flash",
            "newer": f"device {current_version!r} is newer than {version!r} - "
                     f"refusing automatic downgrade",
            "unknown": f"cannot prove {version!r} is an upgrade (device reports "
                       f"{current_version!r}) - skipping; recovery needs force",
        }[decision]
        print(f"[flash] {explain}")
        return 0, provenance

    print(f"[flash] {decision}: {current_version!r} -> {version!r}")
    try:
        helpers.flash_ambit_firmware(firmware_dir, port=port)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"[flash] flashing failed: {exc}")
        return 1, provenance

    time.sleep(1.0)
    helpers.invalidate_port_cache()
    return 0, provenance


def _adpd_trace_meta(arrun):
    """The per-trace facts worth keeping once the samples are dropped.

    ``pulse_currents_zeroed`` is the one that must not be lost: firmware that
    answers ``set_currents`` with BAD COMMAND leaves the ADPD pulse LEDs driving
    during the trace, so `leaf` / `sun` stop being a measurement of the incident
    light. Reduced statistics look exactly the same either way, which is why the
    flag travels beside them rather than inside the samples that were discarded.
    """
    if not arrun:
        return None
    return {k: arrun.get(k) for k in ("actinic", "num_points", "freq_hz",
                                      "pulse_currents_zeroed", "truncated")}


def _pulse_leds_confirmed(*records):
    """Did every recorded trace confirm zeroed pulse LEDs? None if none ran."""
    flags = [meta["pulse_currents_zeroed"]
             for record in records if record
             for meta in _iter_trace_meta(record)]
    return all(flags) if flags else None


def _iter_trace_meta(record):
    """Every ``adpd_trace`` block in a sweep record, whichever sweep it is."""
    for point in record.get("sweep") or []:
        if point.get("adpd_trace"):
            yield point["adpd_trace"]
    for meta in record.get("adpd_trace") or []:
        if meta:
            yield meta


def _warn_if_pulse_leds_live(record, tag):
    """Say once, next to the table, that the numbers above include the pulse LEDs.

    The device reports this per trace. Six repeated `unexpected set_currents echo`
    warnings say the command failed but not what it costs the numbers, and the
    reduced statistics look identical either way.
    """
    if _pulse_leds_confirmed(record) is False:
        print(f"[{tag}] the device did NOT confirm zeroed ADPD pulse LEDs "
              f"(set_currents rejected), so leaf/sun above include light from the "
              f"Ambit's own pulse LEDs - treat them as contaminated")


def _print_adpd_table(summaries, level_label, levels):
    """One row per sweep point with the ADPD channel statistics.

    ``leaf`` first: it is the channel the operator watches, and its spread across
    the pulses at a fixed light level is the quickest read on whether the
    detector is behaving.
    """
    channels = quality.ARRUN_CHANNELS
    header = f"{level_label:>10} | " + " | ".join(f"{c:>19}" for c in channels)
    print(header)
    print("-" * len(header))
    for level, stats in zip(levels, summaries):
        cells = []
        for channel in channels:
            s = (stats or {}).get(channel)
            if not s:
                cells.append(f"{'-':^19}")
                continue
            mark = "!" if s["saturated"] else " "
            # 10 + 2 + 6 + 1 = 19, wide enough for a pinned 24-bit sum.
            cells.append(f"{s['mean']:>10.1f}+-{s['std']:<6.1f}{mark}")
        print(f"{level:>10} | " + " | ".join(cells))
    print(f"({ARRUN_PULSES} pulses per point at {ARRUN_FREQ_HZ} Hz; mean +- sd; "
          f"! = pinned at full scale)")
    # env is the MLX object temperature, sampled once per trace rather than once
    # per pulse, so its +-0.0 is arithmetic on one value - and the object cannot
    # warm measurably inside a 0.5 s trace anyway. Said here so the zero is not
    # read as an unusually quiet photodiode.
    print("(env is the MLX object temperature: one value per trace, so +-0.0 is "
          "expected)")


# ============================================================================
# Tier 3: the only PAR calibration
# ============================================================================

def _read_sweep_point(link, port_ref, current, *, adpd=RECORD_ADPD_TRACES):
    """One sweep point: reference, the Ambit's cmd-35 read, then the ADPD trace.

    The reference is read first and the Ambit second, so the two straddle the
    same lamp state as tightly as the ~284 ms cmd-35 measurement allows. The ADPD
    trace comes last on purpose: it zeroes the pulse LEDs and drives the actinic,
    both of which change what the AS7341 would see, so it must not precede the
    spectral read it is being paired with.
    """
    ref_par = helpers.get_par_MP(port_ref)
    ref_spec = helpers.get_spec_raw_MP(port_ref)
    reading = link.spec_raw()

    # actinic=0: the tier-3 sweep characterises the lamp, so the Ambit's own LED
    # stays off and leaf/sun measure the same light the reference is measuring.
    arrun = link.arrun(actinic=0, num_points=ARRUN_PULSES,
                       freq=ARRUN_FREQ_HZ) if adpd else None

    usable, reasons = spec_cal.usable_for_fit(reading)
    point = {
        "current_A": current,
        "ref_par": float(ref_par),
        "ref_spec_minipar_order": (ref_spec or {}).get("counts"),
        "ref_spec_ambit_order": helpers.minipar_to_ambit_order((ref_spec or {}).get("counts")),
        "ambit": reading.to_dict(),
        # Only the reduction plus the trace's own provenance is kept. The raw
        # pulse samples are ~300 B per point and nothing downstream reads them.
        "adpd_stats": quality.summarize_arrun(arrun),
        "adpd_trace": _adpd_trace_meta(arrun),
        "usable": usable,
        "rejected_because": reasons,
    }
    return point, reading


def calibrate_tier3(port_ambit, port_ref, port_dc, *,
                    currents=PAR_CAL_CURRENTS, upload=UPLOAD_COEFFICIENTS,
                    reference=None):
    """Sweep the lamp, fit ``par = a * par_tier2 + b``, write and verify.

    ``par_tier2`` is the regressand input rather than ``par``: it is the tier-2
    quantity before slope/intercept, so the sweep does not have to reset the
    device's existing tier 3 first and the fit cannot be a slope on top of a
    slope (plan section 5).

    :return: a JSON-ready record of the sweep, the fit, the read-back and the
        post-write confirmation
    """
    record = {
        "kind": "tier3_par",
        "reference": reference,
        "seed_generation": spec_cal.SEED_GENERATION,
        "uploaded": False,
    }

    # ---- capability probe, before touching the lamp -----------------------
    ok, detail = helpers.probe_cmd35(port_ambit)
    record["cmd35_probe"] = {"ok": ok, "detail": detail}
    if not ok:
        print(f"[tier3] SKIPPED - {detail}")
        print("[tier3] this firmware has no three-tier cmd 35; there is no legacy "
              "fallback in this version of the Calibratron by design")
        record["status"] = "unsupported_firmware"
        return record
    print(f"[tier3] {detail}")

    with helpers.AmbitLink(port_ambit) as link:
        # ---- read-back before anything changes ---------------------------
        before = link.spec_cal()
        record["spec_cal_before"] = before.to_dict()
        probe = link.spec_raw()
        provisional = spec_cal.read_par_provisional(probe, before)
        record["par_provisional_before"] = provisional
        record["flags_before"] = {
            "raw": probe.flags,
            "names": probe.flag_names(),
            "par_weight_is_fleet_fit": probe.par_weight_is_fleet_fit,
            "tier3_stored": probe.tier3_stored,
        }
        if not provisional["flag_vector_agreement"]:
            # The firmware's calibration bits and the NVS vectors disagree. Do not
            # pick a winner silently - a sweep taken on top of an unknown tier-2
            # state is not a calibration.
            print("[tier3] ABORT - firmware flags disagree with the read-back vectors")
            record["status"] = "flag_vector_disagreement"
            return record
        print(f"[tier3] tier 3 on the device: par_slope={before.par_slope:.6g}, "
              f"par_intercept={before.par_intercept:.6g}"
              f"{'  (identity - never swept)' if before.tier3_is_identity() else ''}")
        for reason in provisional["reasons"]:
            print(f"[tier3]   provisional: {reason}")

        # ---- verify the firmware's own arithmetic ------------------------
        # The whole point of keeping raw[] on the wire. One check covers the ms
        # tick, the gain ordinal, the per-bank divisor and the NIR/Clear swap.
        check_reading = link.spec_raw()
        math_check = spec_cal.verify_firmware_math(check_reading, before,
                                                   rtol=MATH_CHECK_RTOL)
        record["firmware_math_check"] = math_check
        if math_check["passed"]:
            print(f"[tier3] firmware math reproduced host-side "
                  f"(max rel. error {math_check['max_rel_error']:.2e})")
        else:
            print("[tier3] FIRMWARE MATH MISMATCH - the fit below would encode the bug:")
            for line in math_check["mismatches"][:6]:
                print(f"[tier3]   {line}")
            record["status"] = "firmware_math_mismatch"
            return record

        # ---- the sweep ---------------------------------------------------
        # The lamp is driven here, so the shutdown lives in `finally`: an
        # exception on any point must not leave the fixture lit.
        points = []
        try:
            for current in currents:
                helpers.set_current(port_dc, current)
                time.sleep(LAMP_SETTLE_S)
                point, reading = _read_sweep_point(link, port_ref, current)
                points.append(point)
                flag = "" if point["usable"] else f"   REJECTED: {'; '.join(point['rejected_because'])}"
                print(f"[tier3]   {current:4.1f} A   par_tier2={reading.par_tier2:10.3f}   "
                      f"ref={point['ref_par']:8.2f}{flag}")
        except helpers.ReferenceUnavailable as exc:
            # Nothing is written on this path: a partial sweep against a
            # reference that stopped answering is not a calibration.
            print(f"[tier3] ABORT - {exc}")
            record["sweep"] = points
            record["status"] = "reference_unavailable"
            return record
        finally:
            helpers.set_current(port_dc, 0.0)

    record["sweep"] = points
    record["spectral_drift"] = spec_cal.spectral_drift(
        [p["ref_spec_ambit_order"] for p in points if p["ref_spec_ambit_order"]])

    if any(p["adpd_stats"] for p in points):
        # Reported, never a gate: these traces are for later analysis, so a flat
        # or pinned photodiode must not discard an otherwise good tier-3 fit.
        record["adpd_sweep"] = quality.assess_adpd_sweep(
            [p["adpd_stats"] for p in points], [p["current_A"] for p in points],
            responders=TIER3_ADPD_RESPONDERS)
        print("\n[tier3] ADPD photodiodes vs lamp current (actinic off):")
        _print_adpd_table([p["adpd_stats"] for p in points], "lamp A",
                          [p["current_A"] for p in points])
        _warn_if_pulse_leds_live(record, "tier3")
        for channel, summary in record["adpd_sweep"].items():
            for note in summary.get("notes", []):
                print(f"[tier3]   {channel}: {note}")
        print("[tier3] recorded for later analysis - nothing is fitted from these")

    usable = [p for p in points if p["usable"]]
    if len(usable) < len(points):
        print(f"[tier3] {len(points) - len(usable)} of {len(points)} points rejected")

    # Named `fit`, not `quality`: `quality` is the module this function calls for
    # the ADPD table above, and binding it locally here made that call an
    # UnboundLocalError at the *top* of the function - after a full sweep.
    fit = spec_cal.assess_affine_fit(
        [p["ambit"]["par_tier2"] for p in usable],
        [p["ref_par"] for p in usable],
        [p["current_A"] for p in usable],
    )
    record["fit"] = fit
    print(f"[tier3] fit: par_slope={fit['par_slope']:.6g}  "
          f"par_intercept={fit['par_intercept']:.6g}  "
          f"R^2={fit['r2']:.6f}  NRMSE={fit['nrmse']:.4f}")
    for note in fit["notes"]:
        print(f"[tier3]   note: {note}")

    if not fit["passed"]:
        print("[tier3] REJECTED; existing tier 3 kept: " + "; ".join(fit["reasons"]))
        record["status"] = "rejected"
        return record
    if not upload:
        print("[tier3] preview only - nothing written")
        record["status"] = "preview"
        return record

    # ---- write and verify --------------------------------------------------
    verified = helpers.write_tier3_with_readback(
        port_ambit,
        par_slope=fit["par_slope"], par_intercept=fit["par_intercept"],
        previous=before.tier3(),
    )
    record["spec_cal_after"] = verified.to_dict()
    record["uploaded"] = True
    record["status"] = "uploaded"

    # ---- closed-loop confirmation -----------------------------------------
    # The fit says the device *should* now agree with the reference. Check it,
    # rather than inferring it from the fit residuals it was derived from.
    confirm = []
    try:
        with helpers.AmbitLink(port_ambit) as link:
            for current in PAR_CONFIRM_CURRENTS:
                helpers.set_current(port_dc, current)
                time.sleep(LAMP_SETTLE_S)
                point, reading = _read_sweep_point(link, port_ref, current)
                ref = point["ref_par"]
                point["rel_error"] = (reading.par - ref) / ref if ref else None
                confirm.append(point)
                shown = "n/a" if point["rel_error"] is None else f"{point['rel_error']:+.2%}"
                print(f"[tier3]   confirm {current:4.1f} A   par={reading.par:8.2f}   "
                      f"ref={ref:8.2f}   {shown}")
    except helpers.ReferenceUnavailable as exc:
        # The write already happened and was read back, so this is a lost check,
        # not a lost calibration. Say which of the two it is.
        print(f"[tier3] confirmation incomplete - {exc}")
        record["confirmation_incomplete"] = str(exc)
    finally:
        helpers.set_current(port_dc, 0.0)
    record["confirmation"] = confirm

    worst = max((abs(p["rel_error"]) for p in confirm if p["rel_error"] is not None),
                default=None)
    record["confirmation_worst_rel_error"] = worst
    if worst is not None and worst > 0.05:
        warnings.warn(f"tier 3 was written and verified, but the device still "
                      f"disagrees with the reference by up to {worst:.1%}")

    # flags bit8 stays CLEAR on a correctly calibrated device: it reports whether
    # the compiled-in par_weight is an ambit fleet fit, and
    # AMBIT_PAR_WEIGHT_IS_AMBIT_FIT is false until the ambit Li-250A campaign
    # lands. So the device remains "PAR provisional" after a good sweep, with
    # bit9 newly set. Recorded, never used as a success criterion - the success
    # criterion is the cmd 33/4 read-back plus the confirmation pass above.
    record["par_provisional_after"] = spec_cal.read_par_provisional(
        helpers.get_spec_raw(port_ambit), verified)
    return record


# ============================================================================
# Actinic LED
# ============================================================================

def _read_emit_reference(port_emit):
    """The Emit_LED MiniPAR's calibrated PAR and its raw channel counts."""
    par = helpers.get_par_MP(port_emit)
    return float(par), (helpers.get_spec_raw_MP(port_emit) or {}).get("counts")


def _led_reference_sweep(link, port_emit, settings, measured, ref_spec, latch,
                         *, attempts=LED_LATCH_ATTEMPTS):
    """Pass 1 of the LED sweep: latch each setting, read the reference.

    Deliberately free of any ``arrun`` call, and that separation is the whole
    point of splitting the sweep in two. Interleaved, the sequence per point was
    latch -> read reference -> ADPD trace, and the ADPD trace is ``arrun2``,
    which passes ``persist=0`` and therefore ends with ``AS_LED_OFF()``
    (ambit/src/PAM.cpp). Relighting then depended on the next ``arrun1``
    surviving a 10 ms-per-field parse (see :meth:`helpers.AmbitLink.set_actinic`)
    immediately after a run that had just streamed a burst of data back.

    The 2026-08-18 sweep is what that looks like when it loses: the reference saw
    the LED at the first setting only (216.98 umol) and read the dark floor at
    every setting after it - 5.91 six times, identical to the second decimal,
    with the raw channels flat at [1,3,4,6,7,10,11,8,22,3]. The ADPD ``leaf``
    channel meanwhile tracked the drive perfectly (790 -> 1148 across 0 -> 250),
    because ``arrun2`` drives the actinic itself for the length of its own trace
    and needs no latch. So the LED, the fixture and the settle time were all
    fine; only the latch the reference read depends on was gone.

    The fit gate then reported five simultaneous failures - non-monotonic,
    R^2 -0.96, nRMSE, residual, intercept - which is what a gate does when the
    regressor is a constant. None of them named the cause, so the latch is now
    verified against the reference and re-asserted when it did not take.

    :param measured: caller's list, appended in place so a mid-sweep
        ``ReferenceUnavailable`` still leaves the caller the partial sweep
    :return: the dark-floor record the responses were judged against
    """
    link.set_actinic(0)
    time.sleep(LED_SETTLE_S)
    dark, dark_counts = _read_emit_reference(port_emit)
    threshold = max(LED_MIN_RESPONSE, LED_MIN_RESPONSE_FRACTION * abs(dark))
    print(f"[LED]   dark floor ref={dark:8.2f}  (lit means above "
          f"{dark + threshold:.2f})")

    for setting in settings:
        expect_light = setting > LED_OFF_MAX_SETTING
        for attempt in range(1, max(1, attempts) + 1):
            ran = link.set_actinic(setting)
            time.sleep(LED_SETTLE_S)
            par, counts = _read_emit_reference(port_emit)
            lit = (par - dark) > threshold
            if lit or not expect_light:
                break
            print(f"[LED]   setting {setting:4d}   ref={par:8.2f}  no response "
                  f"above the dark floor - re-asserting the latch "
                  f"({attempt}/{max(1, attempts)})")
        measured.append(par)
        ref_spec.append(counts)
        latch.append({"setting": setting, "attempts": attempt,
                      "run_confirmed": ran, "expected_light": expect_light,
                      "lit": lit})
        note = "" if (lit or not expect_light) else "   LATCH FAILED"
        print(f"[LED]   setting {setting:4d}   ref={par:8.2f}{note}")

    return {"par": dark, "counts": dark_counts, "response_threshold": threshold}


def _led_adpd_sweep(link, settings):
    """Pass 2 of the LED sweep: one ADPD trace per setting.

    Runs after pass 1 rather than inside it. ``arrun2`` drives the actinic itself
    for the length of its own trace, so this pass needs no latch and - now that
    it is separate - cannot destroy the one pass 1 depends on. Here the Ambit's
    own LED *is* the light source, so ``leaf`` and ``sun`` see what the reference
    MiniPAR saw over the LED in pass 1.
    """
    arruns = [link.arrun(actinic=setting, num_points=ARRUN_PULSES,
                         freq=ARRUN_FREQ_HZ)
              for setting in settings]
    link.set_actinic(0)              # park the LED off before releasing the link
    return arruns


def calibrate_led(port_ambit, port_emit, *, settings=LED_CAL_SETTINGS,
                  upload=UPLOAD_COEFFICIENTS, current_coeff=None):
    """Sweep the actinic LED and fit the setting against the measured PAR.

    Two passes over the settings, not one: :func:`_led_reference_sweep` reads the
    Emit_LED MiniPAR at every setting, then :func:`_led_adpd_sweep` records the
    ADPD traces. That costs one extra pass and buys a reference sweep no
    ``arrun2`` can darken half way through - see ``_led_reference_sweep`` for the
    failure that motivated it.

    Unlike tier 3 this genuinely passes through the origin - zero drive is zero
    light, with no offset to absorb - so an origin fit is right here, and a free
    intercept is used as evidence against the model rather than fitted.

    Worth knowing what this coefficient does: as of ambit@fix/spec_overflow,
    ``actinic_coef`` is validated, persisted and printed but never *applied* to
    anything - ``AS_LED_Current()`` takes the requested setting directly. So this
    step records a characterisation of the LED rather than changing how the device
    behaves. Still worth running, and worth not over-trusting.
    """
    measured, ref_spec, latch = [], [], []
    dark = None
    # One held-open link for both passes: closing and reopening with DTR/RTS
    # asserted resets the device and drops the latched LED, which is why the old
    # bench script had to order its reads so carefully.
    with helpers.AmbitLink(port_ambit) as link:
        try:
            dark = _led_reference_sweep(link, port_emit, settings,
                                        measured, ref_spec, latch)
        except helpers.ReferenceUnavailable as exc:
            # Closing the link below drops the latched LED, so there is
            # nothing to switch off here - only a record to keep honest.
            print(f"[LED] ABORT - {exc}")
            return {"kind": "actinic_led", "status": "reference_unavailable",
                    "reason": str(exc),
                    "led_settings": list(settings[:len(measured)]),
                    "ref_par": [float(v) for v in measured],
                    "latch": latch, "dark_reference": dark,
                    "act_led_coeff_before": current_coeff, "uploaded": False}
        arruns = (_led_adpd_sweep(link, settings) if RECORD_ADPD_TRACES
                  else [None] * len(settings))

    unlatched = [e["setting"] for e in latch if e["expected_light"] and not e["lit"]]
    if unlatched:
        # Said before the fit, because the fit's five simultaneous failures
        # describe a constant regressor and say nothing about why it was constant.
        print(f"[LED] the reference saw no light at settings {unlatched} - the "
              f"actinic latch did not hold there, so the fit below is measuring "
              f"the dark floor, not the LED")

    adpd_stats = [quality.summarize_arrun(a) for a in arruns]
    fit = quality.assess_led_fit(measured, settings)
    record = {
        "kind": "actinic_led",
        "led_settings": list(settings),
        "ref_par": [float(v) for v in measured],
        "ref_spec_minipar_order": ref_spec,
        "adpd_stats": adpd_stats,
        "adpd_trace": [_adpd_trace_meta(a) for a in arruns],
        "adpd_sweep": (quality.assess_adpd_sweep(adpd_stats, settings,
                                                 responders=LED_ADPD_RESPONDERS)
                       if any(adpd_stats) else None),
        "fit": fit,
        "latch": latch,
        "latch_failed_at": unlatched,
        "dark_reference": dark,
        "act_led_coeff_before": current_coeff,
        "uploaded": False,
    }
    if any(adpd_stats):
        print("\n[LED] ADPD photodiodes vs actinic setting:")
        _print_adpd_table(adpd_stats, "actinic", settings)
        _warn_if_pulse_leds_live(record, "LED")
        for channel, summary in (record["adpd_sweep"] or {}).items():
            for note in summary.get("notes", []):
                print(f"[LED]   {channel}: {note}")
    print(f"[LED] fit: coeff={fit['coefficient']:.6g}  R^2={fit['r2']:.6f}"
          f"  (current act_led_coeff={current_coeff})")
    for note in fit["notes"]:
        print(f"[LED]   note: {note}")

    if not fit["passed"]:
        print("[LED] REJECTED; existing gain kept: " + "; ".join(fit["reasons"]))
        return record
    if not upload:
        print("[LED] preview only - nothing written")
        return record

    helpers.set_ambit_led_gain(port_ambit, fit["coefficient"])
    observed = helpers.ambit_reboot(port_ambit).act_led_coeff
    if abs(observed - fit["coefficient"]) > 1e-3 * max(1.0, abs(fit["coefficient"])):
        raise RuntimeError(f"act_led_coeff write was not verified (read {observed:.6g}, "
                           f"expected {fit['coefficient']:.6g})")
    record["act_led_coeff_after"] = float(observed)
    record["uploaded"] = True
    print(f"[LED] verified persisted gain: {current_coeff} -> {observed:.4f}")
    return record


# ============================================================================
# ADPD dark baseline
# ============================================================================

def calibrate_adpd_baseline(port_ambit, previous, *, upload=UPLOAD_COEFFICIENTS,
                            input_fn=input):
    """Measure a dark baseline and persist all six channels after QC."""
    answer = input_fn("Install the dark fixture, block ambient light, then type DARK "
                      "to measure the six-channel ADPD baseline (anything else skips): ")
    if answer.strip() != "DARK":
        print("[ADPD] skipped; no calibration value was changed")
        return {"kind": "adpd_baseline", "status": "skipped",
                "reason": "dark fixture not confirmed"}

    measured = helpers.measure_adpd_baseline(port_ambit)
    gate = quality.assess_adpd_baseline(measured, previous)
    record = {
        "kind": "adpd_baseline",
        "status": "passed" if gate["passed"] else "rejected",
        "measured": measured,
        "previous": list(previous),
        "quality": gate,
        "uploaded": False,
    }
    if not gate["passed"]:
        print("[ADPD] rejected: " + "; ".join(gate["reasons"]))
        return record
    if not upload:
        print(f"[ADPD] preview only: {measured}")
        return record

    helpers.set_adpd_baseline(port_ambit, measured)
    observed = list(helpers.ambit_reboot(port_ambit).adpd_calibration)
    if observed != measured:
        restore_error = None
        try:
            helpers.set_adpd_baseline(port_ambit, list(previous))
            restored = list(helpers.ambit_reboot(port_ambit).adpd_calibration)
            if restored != list(previous):
                restore_error = f"restore read back {restored!r}"
        except Exception as exc:
            restore_error = str(exc)
        detail = (f"; previous baseline restoration failed: {restore_error}"
                  if restore_error else "; previous baseline restored")
        raise RuntimeError(f"ADPD baseline write was not verified (read {observed!r}, "
                           f"expected {measured!r}){detail}")
    record["uploaded"] = True
    record["readback"] = observed
    print(f"[ADPD] saved and verified: {observed}")
    return record


# ============================================================================
# Runner
# ============================================================================

#: Printed instead of running. Names the ports that answered as well as the ones
#: that did not: "dc missing" and "dc answered on a port you did not expect" look
#: the same to an operator reading one line of output.
ABORT_MISSING_DEVICES = """
[abort] no port answered for: {missing}
        found: {found}
        Any "Cannot open ..." warnings above name ports that are present but
        unopenable - usually another process still holding one, or a USB bridge
        that needs re-plugging. Instruments answer on whatever port Windows
        gave them, so a port that moved is normal; a port that will not open is
        not.
        Set REQUIRE_ALL_DEVICES = False to run without them anyway."""


def main():
    # 1. Find everything on the bus in one pass, cached ports first.
    t0 = time.perf_counter()
    ports = helpers.discover_roles(helpers.DEVICE_SPECS)
    print(f"[discover] {time.perf_counter() - t0:.2f}s  "
          + "  ".join(f"{r}={p or '-'}" for r, p in ports.items()))

    missing = [role for role, port in sorted(ports.items()) if port is None]
    if missing and REQUIRE_ALL_DEVICES:
        # Before the reboot dump and before the rename prompt: nothing has been
        # touched yet, so this is a clean stop rather than a half-run.
        found = ", ".join(f"{r}={p}" for r, p in sorted(ports.items()) if p)
        raise SystemExit(ABORT_MISSING_DEVICES.format(
            missing=", ".join(missing), found=found or "nothing"))

    port_ambit = ports["ambit"]
    if port_ambit is None:
        raise SystemExit("No Ambit device found on any serial port")
    port_ref, port_emit, port_dc = ports["par_ref"], ports["emit_led"], ports["dc"]

    # 2. As-received state, then the one manual question, out of the way early.
    info_asreceived = helpers.ambit_reboot(port_ambit)
    print(info_asreceived)
    fw_asreceived = info_asreceived.firmware
    current_name = info_asreceived.device_name

    new_name = None
    if RENAME_AMBIT:
        new_name = input(f"\nEnter new name for Ambit (current: {current_name}, "
                         f"blank to keep): ").strip() or None

    # 3. Firmware.
    provenance = None
    if FLASH_FIRMWARE:
        print("\n=== Firmware ===")
        if FORCE_FLASH_FIRMWARE:
            print("WARNING: FORCE_FLASH_FIRMWARE - equivalent firmware may be "
                  "re-flashed or an unresponsive device recovered")
        rc, provenance = flash_firmware(port_ambit, force=FORCE_FLASH_FIRMWARE,
                                        current_version=fw_asreceived or None,
                                        allow_downgrade=ALLOW_FIRMWARE_DOWNGRADE)
        if rc != 0:
            # A responding Ambit can still be calibrated, so an unreachable
            # release (offline bench) must not look like a failed calibration.
            print("[flash] continuing with the firmware already on the device")
        else:
            ports = helpers.discover_roles(helpers.DEVICE_SPECS)
            port_ambit = ports["ambit"] or port_ambit
            port_ref, port_emit, port_dc = ports["par_ref"], ports["emit_led"], ports["dc"]

    info_before = helpers.ambit_reboot(port_ambit)

    # 4. Name. After the pre-calibration snapshot, so the record's device_before
    #    diff carries what the device used to be called.
    if new_name:
        helpers.set_ambit_name(port_ambit, new_name)
        print(f"[name] {current_name!r} -> {new_name!r}")

    # 5. Both MiniPARs are snapshotted, not just Par_REF. The Emit_LED unit is
    #    the sole reference behind act_led_coeff, so its coefficients and its own
    #    slope/intercept decide what that number means - and either unit can be
    #    recalibrated between runs, which is exactly what makes the snapshot worth
    #    storing per run rather than assuming.
    reference = helpers.read_minipar_reference(port_ref) if port_ref else None
    reference_emit = helpers.read_minipar_reference(port_emit) if port_emit else None

    # 6. Tier-3 PAR calibration.
    tier3_cal = None
    if CALIBRATE_TIER3 and port_ref and port_dc:
        print("\n=== Tier-3 PAR calibration (par_slope, par_intercept) ===")
        tier3_cal = calibrate_tier3(port_ambit, port_ref, port_dc, reference=reference)
    elif CALIBRATE_TIER3:
        missing = ", ".join(n for n, p in (("Par_REF MiniPAR", port_ref),
                                           ("Kiprim DC source", port_dc)) if p is None)
        print(f"\n[skip] tier-3 PAR calibration - missing: {missing}")

    # 7. Actinic LED.
    led_cal = None
    if CALIBRATE_LED and port_emit:
        print("\n=== Actinic LED calibration ===")
        led_cal = calibrate_led(port_ambit, port_emit,
                                current_coeff=info_before.act_led_coeff)
    elif CALIBRATE_LED:
        print("\n[skip] actinic LED calibration - missing: Emit_LED MiniPAR")

    # 8. ADPD dark baseline (needs an explicitly confirmed dark fixture).
    baseline_cal = None
    if CALIBRATE_ADPD_BASELINE:
        print("\n=== ADPD dark baseline ===")
        baseline_cal = calibrate_adpd_baseline(
            port_ambit, helpers.ambit_reboot(port_ambit).adpd_calibration)

    # 9. Final state.
    print("\n=== Ambit after calibration ===")
    info_after = helpers.ambit_reboot(port_ambit)
    print(info_after)
    try:
        final_cal = helpers.get_spec_cal(port_ambit)
        print(f"Spectral/PAR cal: par_slope={final_cal.par_slope:.6g}, "
              f"par_intercept={final_cal.par_intercept:.6g}, "
              f"seed_match={final_cal.seed_match()}")
    except Exception as exc:
        # Not fatal: the boot dump above is complete, and on firmware without
        # cmd 33/4 there is nothing to read. Say so rather than dying at step 9.
        print(f"[readback] spectral/PAR calibration unreadable: {exc}")
        final_cal = None

    # 10. Record.
    print("\n=== Calibration record ===")
    payload = helpers.make_calibration_payload(
        info_before, info_after,
        spec_par_cal=tier3_cal, led_cal=led_cal, baseline_cal=baseline_cal,
        protocol_id=OJII_PROTOCOL_ID,
        station={
            "firmware_as_received": fw_asreceived,
            "firmware_release_provenance": provenance,
            "par_reference": reference,
            "led_reference": reference_emit,
            "spec_cal_final": final_cal.to_dict() if final_cal else None,
            "ambit_spec_channels": list(spec_cal.CHANNELS),
            "seed_generation": spec_cal.SEED_GENERATION,
            "adpd_traces": ({"channels": list(quality.ARRUN_CHANNELS),
                             "pulses": ARRUN_PULSES, "freq_hz": ARRUN_FREQ_HZ,
                             "pulse_leds": "zeroing requested before every trace",
                             # Requested is not done: firmware that rejects
                             # set_currents leaves them driving, and then leaf/sun
                             # are contaminated. Report what the device confirmed.
                             "pulse_leds_zeroed_confirmed":
                                 _pulse_leds_confirmed(tier3_cal, led_cal),
                             "stored": "per-point statistics and trace provenance "
                                       "only; raw pulse samples are discarded",
                             "purpose": "recorded for later analysis; nothing is "
                                        "fitted or written from them"}
                            if RECORD_ADPD_TRACES else None),
            "calibrated_here": ["par_slope", "par_intercept"],
            "shipped_as_firmware_defaults": ["spec_offset", "spec_sens", "par_weight"],
        },
    )

    path = helpers.save_payload(payload, mac=info_after.MAC, directory=CALIBRATIONS_DIR)

    if not PUBLISH_TO_OPENJII:
        print("[publish] PUBLISH_TO_OPENJII is False - not uploading")
        return payload

    topic = (f"experiment/data_ingest/v1/{OJII_EXPERIMENT_ID}/{OJII_SENSOR_FAMILY}/"
             f"{OJII_SENSOR_VERSION}/{OJII_SENSOR_ID}/{OJII_PROTOCOL_ID}")
    try:
        helpers.publish_payload_mqtt5(payload, topic=topic,
                                      certs_dir=OJII_CERTS_DIR, endpoint=OJII_ENDPOINT)
        print("[publish] uploaded to openJII")
    except Exception as exc:
        # The calibration is already on disk, so a network problem must not look
        # like a failed calibration.
        warnings.warn(f"openJII upload failed ({exc}); the calibration is saved at {path}")
    return payload


if __name__ == "__main__":
    main()
