"""Numerical reduction and gates for everything outside the cmd-35 PAR chain.

The actinic-LED gain, the ADPD dark baseline, and the ADPD photodiode traces
recorded alongside both sweeps. Tier 3's gate lives in
:func:`spec_cal.assess_affine_fit` and stays there: it fits two parameters
against a different threshold set, and the whole point of separating them is
that an origin-forced gate is wrong for tier 3 (see that function's docstring).

Replaces the repo-root ``calibration_quality.py`` for this package. That module
is still correct arithmetic and the legacy ``run_Calibratron.py`` still uses it,
but it had four problems for the new approach:

1. **Its low coefficient bound was inclusive where the firmware's is exclusive.**
   ``valid_actinic_coefficient`` is ``> 0.01 && <= 1.0``
   ([ambit/src/calibration_math.h:32](../../ambit/src/calibration_math.h)), and
   ambit's own test asserts "actinic lower bound is exclusive". The host gate
   was ``coefficient_min <= coefficient``, so a fit of exactly 0.01 passed
   host-side QC and was then refused by the device - the same class of bug as
   sending an unrounded ``par_slope``. Bounds here mirror the predicate exactly.
2. **One of its gates was dead at the LED call site.** It took ``stimulus`` as a
   third argument to order the sweep, but ``calibrate_led`` passed the LED
   settings as *both* ``y_values`` and ``stimulus``, so "reference readings are
   not monotonic with the applied stimulus" compared the settings to themselves
   and could never fire. :func:`assess_led_fit` takes two arguments, which makes
   that impossible rather than merely unlikely.
3. **A slightly negative dark reading failed the whole sweep closed.** Any
   negative value anywhere tripped "calibration values must be non-negative".
   The MiniPAR reports ``par_raw * slope + intercept``; with a negative
   intercept a genuinely dark reading comes back a shade below zero, which is
   noise, not bad data. Small negatives inside a tolerance are now accepted.
4. **Its threshold constants shared names with different values in
   ``spec_cal``** - both defined ``MIN_R2``, ``MAX_NRMSE`` and
   ``MAX_MONOTONIC_REVERSAL``, at 0.99/0.05/0.02 against 0.995/0.03/0.02.
   Someone tuning "the R-squared gate" had two places to get it wrong. Every
   threshold here is prefixed by the step it belongs to.
"""

from __future__ import annotations

import math
import statistics


# ---- firmware predicates, mirrored ----------------------------------------
# ambit/src/calibration_math.h. Polarity matters: the actinic bound is
# EXCLUSIVE at the low end and inclusive at the high end.
ACTINIC_COEFFICIENT_MIN = 0.01      # exclusive
ACTINIC_COEFFICIENT_MAX = 1.0       # inclusive
MAX_ADPD_BASELINE = 0xFFFFFF        # every channel, unsigned 24-bit
MAX_S630_BASELINE = 400             # s_630 only, the firmware's safety limit
ADPD_CHANNEL_COUNT = 6


def valid_actinic_coefficient(value):
    """Mirror of the firmware predicate: finite, ``(0.01, 1.0]``."""
    return (isinstance(value, (int, float)) and math.isfinite(value)
            and ACTINIC_COEFFICIENT_MIN < value <= ACTINIC_COEFFICIENT_MAX)


def valid_adpd_baseline(values):
    """Mirror of the firmware predicate: six u24, and ``s_630 <= 400``."""
    if len(values) != ADPD_CHANNEL_COUNT:
        return False
    if any(isinstance(v, bool) or not isinstance(v, int)
           or v < 0 or v > MAX_ADPD_BASELINE for v in values):
        return False
    return values[0] <= MAX_S630_BASELINE


# ---- actinic LED ----------------------------------------------------------
LED_MIN_R2 = 0.99
LED_MAX_NRMSE = 0.05
LED_MAX_FULL_SCALE_RESIDUAL = 0.10
LED_MAX_INTERCEPT_FRACTION = 0.05
LED_MAX_MONOTONIC_REVERSAL = 0.02
LED_MIN_POINTS = 4

#: A dark reading may come back this far below zero before it counts as bad data
#: rather than noise, as a fraction of the measured span.
LED_DARK_TOLERANCE = 0.01


def assess_led_fit(par_measured, led_settings):
    """Fit ``led_setting = coefficient * measured_PAR`` through the origin.

    Origin-forced deliberately, and correctly - unlike tier 3. Zero drive is
    zero light with no offset to absorb, so a free intercept here is evidence
    the model is wrong rather than a quantity to fit, and it is used as a gate
    exactly as the repo-root module intended.

    The settings are both the regressand and the stimulus, so they are passed
    once. Monotonicity is therefore only meaningful in one direction - does the
    *measured* PAR rise with the applied setting - and only that is checked.

    Note on the fit direction, inherited from the original bench script and kept
    for continuity: the controlled variable (the setting) is the ``y`` here and
    the measured one (PAR) the ``x``. That yields the best predictor of a setting
    given a wanted PAR, which is the use, but it puts the measurement noise on
    ``x``, so the coefficient is attenuated by roughly the noise-to-signal
    variance ratio. With a repeatable LED and a settled reference that is small;
    it is recorded in the result as ``fit_direction`` so a later reanalysis can
    see which way round it was done.

    :param par_measured: reference PAR at each setting, same order
    :param led_settings: the applied actinic settings
    :return: fail-closed QC record; ``coefficient`` is the value to persist
    """
    x = [float(v) for v in par_measured]
    y = [float(v) for v in led_settings]
    if len(x) != len(y):
        raise ValueError("par_measured and led_settings must be the same length")

    reasons, notes = [], []
    if len(x) < LED_MIN_POINTS:
        reasons.append(f"at least {LED_MIN_POINTS} settings are required, got {len(x)}")

    finite = all(math.isfinite(v) for v in x + y)
    if not finite:
        reasons.append("all calibration values must be finite")

    x_span = (max(x) - min(x)) if finite and x else 0.0
    y_span = (max(y) - min(y)) if finite and y else 0.0
    if x_span <= 0 or y_span <= 0:
        reasons.append("sweep must span a non-zero measured and applied range")

    # A dark reading a shade below zero is the reference's own intercept showing
    # through, not bad data. Clamp it and say so; reject anything beyond that.
    if finite:
        tolerance = LED_DARK_TOLERANCE * x_span
        if any(v < -tolerance for v in x):
            reasons.append("measured PAR is negative beyond the dark tolerance")
        elif any(v < 0 for v in x):
            notes.append("a dark reading was slightly negative and clamped to zero")
            x = [max(0.0, v) for v in x]
    if any(v < 0 for v in y):
        reasons.append("applied LED settings must be non-negative")

    if finite and len(x) >= 2 and x_span > 0:
        order = sorted(range(len(y)), key=y.__getitem__)
        ordered_x = [x[i] for i in order]
        if any(b - a < -LED_MAX_MONOTONIC_REVERSAL * x_span
               for a, b in zip(ordered_x, ordered_x[1:])):
            reasons.append("measured PAR is not monotonic with the applied setting")

    denominator = math.fsum(v * v for v in x) if finite else 0.0
    coefficient = (math.fsum(a * b for a, b in zip(x, y)) / denominator
                   if denominator > 0 else math.nan)

    if math.isfinite(coefficient):
        prediction = [coefficient * v for v in x]
        residual = [a - b for a, b in zip(y, prediction)]
        ss_res = math.fsum(v * v for v in residual)
        y_mean = sum(y) / len(y)
        ss_tot = math.fsum((v - y_mean) ** 2 for v in y)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else math.nan
        nrmse = math.sqrt(ss_res / len(y)) / y_span if y_span > 0 else math.inf
        max_residual_fraction = (max(abs(v) for v in residual) / y_span
                                 if y_span > 0 else math.inf)
    else:
        prediction, residual = [], []
        y_mean = sum(y) / len(y) if y else 0.0
        r2, nrmse, max_residual_fraction = math.nan, math.inf, math.inf

    # Free-intercept fit, kept purely as evidence about the origin model.
    if finite and len(x) >= 2 and x_span > 0:
        x_mean = sum(x) / len(x)
        covariance = math.fsum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
        variance = math.fsum((v - x_mean) ** 2 for v in x)
        free_slope = covariance / variance if variance > 0 else math.nan
        free_intercept = (y_mean - free_slope * x_mean
                          if math.isfinite(free_slope) else math.nan)
        intercept_fraction = (abs(free_intercept) / y_span
                              if math.isfinite(free_intercept) and y_span > 0 else math.inf)
    else:
        free_slope = free_intercept = math.nan
        intercept_fraction = math.inf

    if not valid_actinic_coefficient(coefficient):
        reasons.append(f"act_led_coeff must be finite and within "
                       f"({ACTINIC_COEFFICIENT_MIN}, {ACTINIC_COEFFICIENT_MAX}] - "
                       f"the firmware's low bound is exclusive")
    if not math.isfinite(r2) or r2 < LED_MIN_R2:
        reasons.append(f"R-squared must be at least {LED_MIN_R2}")
    if not math.isfinite(nrmse) or nrmse > LED_MAX_NRMSE:
        reasons.append(f"normalized RMSE must be at most {LED_MAX_NRMSE}")
    if not math.isfinite(max_residual_fraction) or max_residual_fraction > LED_MAX_FULL_SCALE_RESIDUAL:
        reasons.append(f"maximum residual must be at most "
                       f"{LED_MAX_FULL_SCALE_RESIDUAL} of full scale")
    if not math.isfinite(intercept_fraction) or intercept_fraction > LED_MAX_INTERCEPT_FRACTION:
        reasons.append(f"free-fit intercept must be at most "
                       f"{LED_MAX_INTERCEPT_FRACTION} of full scale - a real "
                       f"intercept means the origin model does not hold here")

    return {
        "passed": not reasons,
        "reasons": reasons,
        "notes": notes,
        "fit": "through_origin",
        "fit_direction": "led_setting = coefficient * measured_PAR",
        "coefficient": coefficient,
        "r2": r2,
        "nrmse": nrmse,
        "max_residual_fraction": max_residual_fraction,
        "residual": residual,
        "prediction": prediction,
        "free_slope": free_slope,
        "free_intercept": free_intercept,
        "free_intercept_fraction": intercept_fraction,
        "n_points": len(x),
        "thresholds": {
            "coefficient_min_exclusive": ACTINIC_COEFFICIENT_MIN,
            "coefficient_max_inclusive": ACTINIC_COEFFICIENT_MAX,
            "min_r2": LED_MIN_R2,
            "max_nrmse": LED_MAX_NRMSE,
            "max_full_scale_residual": LED_MAX_FULL_SCALE_RESIDUAL,
            "max_intercept_fraction": LED_MAX_INTERCEPT_FRACTION,
            "max_monotonic_reversal": LED_MAX_MONOTONIC_REVERSAL,
            "min_points": LED_MIN_POINTS,
            "dark_tolerance": LED_DARK_TOLERANCE,
        },
    }


# ---- ADPD dark baseline ---------------------------------------------------

def assess_adpd_baseline(measured, previous):
    """Gate a six-channel ADPD dark baseline before it is persisted.

    Lives here rather than inline in the runner so that every numerical gate the
    bench applies is in one file with the firmware bounds it mirrors. The
    previous vector is checked too, and its failure is fatal: without a readable
    prior value there is nothing to roll back to if the write does not verify.

    :param measured: the six channels just measured
    :param previous: the six channels currently on the device
    :return: fail-closed QC record
    """
    measured = list(measured)
    previous = list(previous)
    reasons = []

    if not valid_adpd_baseline(previous):
        reasons.append("existing six-channel baseline could not be read or is out "
                       "of range; rollback would not be safe")
    if len(measured) != ADPD_CHANNEL_COUNT:
        reasons.append(f"baseline must contain {ADPD_CHANNEL_COUNT} values, "
                       f"got {len(measured)}")
    elif any(isinstance(v, bool) or not isinstance(v, int)
             or v < 0 or v > MAX_ADPD_BASELINE for v in measured):
        reasons.append("baseline must contain six unsigned 24-bit integers")
    elif measured[0] > MAX_S630_BASELINE:
        # The firmware refuses this outright, so catching it here turns a silent
        # rejected write into a reported one - usually a light leak in the fixture.
        reasons.append(f"s_630 dark baseline {measured[0]} exceeds the firmware "
                       f"safety limit of {MAX_S630_BASELINE} - check the dark fixture")

    return {
        "passed": not reasons,
        "reasons": reasons,
        "measured": measured,
        "previous": previous,
        "thresholds": {
            "channels": ADPD_CHANNEL_COUNT,
            "max_baseline": MAX_ADPD_BASELINE,
            "s_630_max": MAX_S630_BASELINE,
        },
    }


# ---- ADPD photodiode traces ------------------------------------------------
#: Channels an ``arrun2`` trace fills. ``s_730`` / ``r_730`` stay empty because
#: the run is issued as type 2 (no IR reflect); ``sun`` and ``leaf`` are
#: populated only because ambient sub-sampling is 1, i.e. sampled at every point.
#: ``leaf`` is listed first because it is the channel an operator watches.
ARRUN_CHANNELS = ("leaf", "sun", "s_630", "r_630", "env")

#: The ADPD sums are unsigned 24-bit - the same ceiling the firmware validates
#: dark baselines against. A sample at or near it means the channel is pinned and
#: the value is a floor, not a measurement.
ADPD_SAMPLE_MAX = MAX_ADPD_BASELINE

#: Within this fraction of full scale, treat a channel as effectively saturated.
ADPD_SATURATION_MARGIN = 0.01


def summarize_arrun(arrun, channels=ARRUN_CHANNELS):
    """Reduce one ADPD trace to per-channel statistics.

    Saturation is reported per channel because a pinned photodiode is the failure
    that quietly ruins a later analysis: the samples stay plausible, the spread
    collapses, and nothing else in the record says the number is a floor rather
    than a measurement.

    ``std`` is the sample standard deviation (n-1), which is 0.0 for a single
    sample rather than undefined - the spread across the pulses at a fixed light
    level is the quickest read on whether the detector is behaving.

    :param arrun: an arrun record from :meth:`helpers.AmbitLink.arrun`, or None
    :return: ``{tag: {"n", "mean", "std", "min", "max", "saturated"}}``; ``{}``
        if there is nothing numeric to reduce
    """
    if not arrun or not arrun.get("data"):
        return {}

    threshold = ADPD_SAMPLE_MAX * (1.0 - ADPD_SATURATION_MARGIN)
    out = {}
    for tag in channels:
        values = arrun["data"].get(tag)
        if not values:
            continue
        # An unparseable payload is kept as text by the reader; skip it rather
        # than letting str/int comparisons raise deep inside a sweep.
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in values):
            continue
        n = len(values)
        out[tag] = {
            "n": n,
            "mean": statistics.fmean(values),
            "std": statistics.stdev(values) if n > 1 else 0.0,
            "min": float(min(values)),
            "max": float(max(values)),
            "saturated": max(values) >= threshold,
        }
    return out


def assess_adpd_sweep(summaries, stimulus, channels=("leaf", "sun"),
                      responders=None):
    """Sanity-check the photodiode response across a whole sweep.

    Reported, never a rejection: these traces are recorded for later analysis and
    are not the calibration being applied, so a flat or reversed channel is
    something the operator should see rather than something that should discard a
    good tier-3 fit.

    ``responders`` names the channels that are *supposed* to see this stimulus,
    and only those get the flat / non-monotonic notes. `leaf` and `sun` face
    different directions, so which one responds is a property of the fixture, not
    of the detector: under the halogen lamp `sun` spans ~927 counts monotonically
    while `leaf` moves ~69 and wanders; under the Ambit's own actinic LED it is
    the reverse (`leaf` 790 -> 1153, `sun` flat at ~660). Flagging the channel
    that is aimed elsewhere reports the geometry as a fault every single run,
    which is how a real flat channel gets lost in the noise. Saturation is still
    reported for every channel - a pinned photodiode is a fault wherever it points.

    :param summaries: per-point output of :func:`summarize_arrun`, sweep order
    :param stimulus: the drive at each point, same order
    :param channels: which channels to report on
    :param responders: which of them face this stimulus (default: all of them)
    :return: ``{channel: {"n_points", "saturated_at", "monotonic", "span", ...}}``
    """
    responders = tuple(channels) if responders is None else tuple(responders)
    order = sorted(range(len(stimulus)), key=lambda i: float(stimulus[i]))
    report = {}
    for channel in channels:
        points = [(float(stimulus[i]), summaries[i].get(channel))
                  for i in order if i < len(summaries) and summaries[i]]
        usable = [(drive, s) for drive, s in points if s]
        if not usable:
            report[channel] = {"n_points": 0, "note": "channel absent from every trace"}
            continue

        means = [s["mean"] for _drive, s in usable]
        span = max(means) - min(means)
        saturated_at = [drive for drive, s in usable if s["saturated"]]
        # Allow a little reversal for noise, scaled to the observed span.
        tolerance = 0.02 * span
        monotonic = all(b - a >= -tolerance for a, b in zip(means, means[1:]))

        notes = []
        if saturated_at:
            notes.append(f"pinned at full scale at drive {saturated_at} - readings "
                         f"there are a floor, not a measurement")
        if channel in responders:
            if span <= 0:
                notes.append("no response across the sweep - check the detector")
            elif not monotonic:
                notes.append("response is not monotonic with the drive")
        report[channel] = {
            "n_points": len(usable),
            "expected_to_respond": channel in responders,
            "drive": [drive for drive, _s in usable],
            "mean": means,
            "span": span,
            "saturated_at": saturated_at,
            "monotonic": monotonic,
            "notes": notes,
        }
    return report
