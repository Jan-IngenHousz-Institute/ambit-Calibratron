"""Ambit cmd-35 spectral/PAR calibration: codecs, tier math and quality gates.

Pure module - no serial, no numpy, no I/O - so every claim it makes about the
firmware can be unit-tested from a byte string. Transport lives in
:mod:`helpers`; the bench sequence lives in :mod:`run_calibratron`.

Implements the host side of ``ambit/plans/AMBIT_COMMAND35_SPECPAR.md``:

  tier 1   x[i] = raw[i] / (gain(bank(i)) * tint_ms)      pure arithmetic
  tier 2   t2   = SUM par_weight[i] * s[i]                fleet, shipped seeded
  tier 3   par  = par_slope * t2 + par_intercept          per device  <- our job

The Calibratron fits **tier 3 only**. `spec_offset`, `spec_sens` and
`par_weight` ship as firmware defaults (plan section 7); this module reads them
back to verify the firmware's arithmetic and to record provenance, and never
fits them - a per-device 10-coefficient tier-2 fit scores R^2 -31 .. -7443
(plan section 2).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass


# ============================================================================
# Constants from the plan
# ============================================================================

#: Integration-time tick, MILLISECONDS (decision 3). Not seconds - the ams
#: constants (dark offsets, reconstruction matrix) are published in ms, and the
#: firmware follows them. Using the seconds tick that miniPar's *firmware* uses
#: puts a silent factor of 1000 between host and device.
SPEC_TICK_MS = 2.78e-3

#: Ambit channel order, from SPEC_RAW_INDEX = {0,1,2,3,6,7,8,9,11,10}. Slot 8 is
#: NIR and slot 9 is Clear - the OPPOSITE of miniPar, whose order ends
#: ``clear, nir``. Every vector below is already in ambit order.
CHANNELS = ("f1_415", "f2_445", "f3_480", "f4_515", "f5_555",
            "f6_590", "f7_630", "f8_680", "nir_910", "clear")

#: miniPar's order, kept only to make a swap explicit where one is needed.
MINIPAR_CHANNELS = ("f1_415", "f2_445", "f3_480", "f4_515", "f5_555",
                    "f6_590", "f7_630", "f8_680", "clear", "nir_910")

N_CHANNELS = len(CHANNELS)

#: Slots 0-3 (F1-F4) divide by gain_low; slots 4-9 divide by gain_high. NIR and
#: Clear are reported from the *high* read (after setGain(gain2)), despite three
#: comments in the ambit repo saying otherwise - plan section 8.
GAIN_HIGH_FROM_SLOT = 4

SPEC_RAW_SIZE = 80      # cmd 35 response payload, plan section 5
SPEC_CAL_SIZE = 132     # cmd 33 subtype 4 response payload, plan section 6.4
SPEC_RAW_FORMAT = 1
SPEC_CAL_FORMAT = 1

# flags bits — read from the IMPLEMENTED firmware (run_esp.cpp cmd 35), which
# supersedes the plan's original single-zone word. Two zones with OPPOSITE
# polarity, kept in separate bytes so neither can be read as the other, and both
# failing safe: an all-zero flags word means "no fault reported, nothing
# confirmed calibrated" — the pessimistic reading a truncated or zeroed frame
# should produce.
#
# low byte — conditions, 1 = needs attention
FLAG_SATURATED = 1 << 0   # a channel hit digital full scale  (== sat_mask != 0)
FLAG_CLIPPED   = 1 << 1   # a channel clipped at its dark offset (== clip_mask != 0)
FLAG_ASAT      = 1 << 2   # AS7341 analog saturation (ASAT, STATUS2)
FLAG_FAULT     = 1 << 3   # acquisition fault (I2C read failed or sensor absent)

# high byte — calibration, 1 = confirmed
FLAG_PAR_WEIGHT_IS_FLEET_FIT = 1 << 8   # 0 = borrowed miniPar seed, PAR provisional
FLAG_TIER3_STORED            = 1 << 9   # 0 = this device has never been swept

CONDITION_FLAGS   = 0x00FF
CALIBRATION_FLAGS = 0xFF00

FLAG_NAMES = {
    FLAG_SATURATED: "digital full scale",
    FLAG_CLIPPED:   "clipped at dark offset",
    FLAG_ASAT:      "analog saturation (ASAT)",
    FLAG_FAULT:     "acquisition fault",
    FLAG_PAR_WEIGHT_IS_FLEET_FIT: "par_weight is an ambit fleet fit",
    FLAG_TIER3_STORED:            "tier 3 stored for this device",
}

#: Conditions that make a reading unusable. The dark-offset clip is handled
#: separately by :func:`usable_for_fit` because it needs its own argument.
FATAL_FLAGS = FLAG_SATURATED | FLAG_ASAT | FLAG_FAULT


# ---- firmware predicates, mirrored from plan section 6.5 -------------------
# Mirrored here so the host never sends a value the device will silently reject.
# The isfinite half is the load-bearing part: it is what keeps a NaN out of the
# tier-1 divide.
PAR_SLOPE_RANGE      = (0.0, 100.0)    # exclusive low, inclusive high
PAR_INTERCEPT_ABSMAX = 500.0
SPEC_OFFSET_RANGE    = (0.0, 1.0)      # [0, 1)
SPEC_SENS_RANGE      = (0.0, 1000.0)   # (0, 1000]
PAR_WEIGHT_ABSMAX    = 1e4


# ---- shipped defaults, plan section 7 -------------------------------------
# Recorded as a cross-check on flags bit8/bit9 and as a provenance record: the
# bits say whether a fit is an ambit one, not WHICH seed it replaced.
# All in ambit channel order.

#: ams workbook AS7341_AD000198_3-00.xlsx, sheet "used Correction Values" row 15.
#: A property of the silicon, so this one is a real default, not a seed.
SEED_SPEC_OFFSET = (0.00196979, 0.00724927, 0.00319381, 0.001314659, 0.001468153,
                    0.001858105, 0.001762778, 0.00521704, 0.001, 0.003)

#: miniPar LR1-B campaign, 3 devices, mean of the per-device factors (7b).
SEED_SPEC_SENS = (34.950663, 65.289484, 72.697997, 63.273264, 56.737110,
                  52.958660, 48.706781, 42.671670, 5.781618, 31.565986)

#: miniPar Li-250A campaign, ``lsq_linear`` on basic counts with **F1-F8 >= 0**
#: and Clear/NIR free, shrunk toward ``W_TARGET`` at lambda=0.03, 462 samples over
#: 6 devices, tier-2 intercept discarded (7c). Leave-one-device-out median 4.38%.
#: Exported by ``miniPar/new_calibration_miniPAR/par_coeffs_fleet.json``.
#:
#: The sign pattern is now the physical one: the eight band channels lift PAR and
#: only NIR subtracts. That is the point of the constraint. The superseded plain
#: OLS vector (see :data:`SUPERSEDED_PAR_WEIGHTS`) put negative weight on F3, F5
#: and F8, which was collinearity - condition number ~451 on a daylight-dominated
#: set - rather than spectral response, and it cost accuracy exactly where the
#: spectrum stops looking like daylight: canopy PAR spread 6.2% unconstrained
#: against 0.4% here, coefficient direction spread p95 62 deg against 8 deg.
#:
#: Still a *seed*: fitted on miniPar optics, so flags bit8 stays clear and PAR
#: stays provisional until an ambit Li-250A campaign lands.
#:
#: One caveat carried from the export: ``prior_is_placeholder`` is true, so the
#: shrinkage target is not yet the computed CM weight vector. Expect one more
#: generation of this seed.
SEED_PAR_WEIGHT = (40.8063026, 42.1780116, 69.8493647, 85.8748691, 61.8955888,
                   43.742399, 51.5713801, 14.7054614, -27.2526003, 19.3083196)

#: Earlier seeds, kept so a device flashed with one is *identified* rather than
#: reported as carrying an unknown measured vector.
#:
#: Without this the transition is silent in the worst direction: after the seed
#: is bumped, every device still on the previous firmware fails
#: :meth:`SpecCal.seed_match` on ``par_weight``, and a False there reads as
#: "someone wrote a fitted vector here" - which is what the field means
#: everywhere else. Provenance is the whole job of these constants, so a
#: recognised old seed must say which one it is.
SUPERSEDED_PAR_WEIGHTS = {
    # Plain OLS, no sign constraint. Unphysical negatives on F3, F5 and F8 and a
    # positive Clear; L1 norm 1172 against 457 here for a 1.8x larger net
    # response, i.e. 2.6x the leverage on spectral shape for the same answer.
    "minipar-2026-08-17-ols": (333.463542, 206.427134, -30.6130744, 283.778061,
                               -144.07319, 73.852848, 38.9285016, -6.43585093,
                               -37.6580353, 16.9097651),
}

TIER3_IDENTITY = (1.0, 0.0)   # par_slope, par_intercept - deliberately not seeded (7d)

#: Provenance string stored with every calibration, so a record says which
#: generation of firmware seeds the tier-3 fit was taken on top of.
SEED_GENERATION = "minipar-2026-08-18-constrained"


# ============================================================================
# Tier 1: exposure normalisation
# ============================================================================

def gain_multiplier(ordinal):
    """``as7341_gain_t`` ordinal -> multiplier, ``n -> 0.5 * 2**n``.

    The wire carries the *ordinal*, not the multiplier (decision 5). Ordinal 0
    is 0.5x, so dividing by the raw byte divides by zero; ordinals 3/4/10 mean
    4x/8x/512x. Ordinal 2 = 2x is the enum's only fixed point, and it is exactly
    the pinned value - so an ordinal bug is invisible at the current exposure.
    """
    ordinal = int(ordinal)
    if not 0 <= ordinal <= 10:
        raise ValueError(f"as7341_gain_t ordinal out of range: {ordinal}")
    return 0.5 * (1 << ordinal)


def integration_time_ms(atime, astep):
    """``(atime + 1) * (astep + 1) * 2.78e-3`` ms. ATIME 99 / ASTEP 499 -> 139 ms."""
    return (int(atime) + 1) * (int(astep) + 1) * SPEC_TICK_MS


def full_scale_counts(atime, astep):
    """ADC full scale at this exposure, capped at the 16-bit counter.

    Computed from the *reported* exposure, never from the firmware's
    compile-time ``SPEC_FULL_SCALE``: that macro is only correct while the
    exposure stays pinned, and cmd 35 reports the exposure precisely so it need
    not stay pinned.
    """
    return min(0xFFFF, (int(atime) + 1) * (int(astep) + 1))


def basic_counts(raw, gain_low, gain_high, atime, astep):
    """Tier 1: ``x[i] = raw[i] / (gain_multiplier(bank(i)) * tint_ms)``.

    :param raw: 10 unscaled counts in ambit channel order
    :return: list of 10 basic counts (ms convention)
    """
    tint = integration_time_ms(atime, astep)
    g_low = gain_multiplier(gain_low) * tint
    g_high = gain_multiplier(gain_high) * tint
    return [float(v) / (g_low if i < GAIN_HIGH_FROM_SLOT else g_high)
            for i, v in enumerate(raw)]


# ============================================================================
# Wire codecs
# ============================================================================

FRAME_START = 0xA0
RESP_START  = 0xA1
RESP_END    = 0xF0

CMD_SPEC_RAW = 35
CMD_INFO     = 33
INFO_SUB_SPEC_CAL = 4


def build_frame(opcode, *args):
    """``0xA0`` + exactly 8 command bytes. Every binary command is this length."""
    body = (int(opcode),) + tuple(int(a) for a in args)
    if len(body) > 8:
        raise ValueError(f"binary command takes at most 8 bytes, got {len(body)}")
    return bytes([FRAME_START]) + bytes(body).ljust(8, b"\x00")


@dataclass
class SpecRaw:
    """One decoded cmd-35 reading (plan section 5)."""

    format: int
    atime: int
    gain_low: int
    gain_high: int
    astep: int
    flags: int
    sat_mask: int
    clip_mask: int
    raw: list                       # 10 x u16, unscaled counts, ambit order
    chan: list                      # 10 x f32, goal A
    par: float                      # goal B, tier 3 applied
    par_tier2: float                # goal B, before slope/intercept  <- the fit input

    # ---- derived ----------------------------------------------------------
    @property
    def tint_ms(self):
        return integration_time_ms(self.atime, self.astep)

    @property
    def full_scale(self):
        return full_scale_counts(self.atime, self.astep)

    @property
    def saturated(self):
        return bool(self.flags & FLAG_SATURATED) or self.sat_mask != 0

    @property
    def clipped(self):
        return bool(self.flags & FLAG_CLIPPED) or self.clip_mask != 0

    @property
    def par_weight_is_fleet_fit(self):
        """bit8: the compiled-in tier-2 vector is an ambit fit, not a miniPar seed."""
        return bool(self.flags & FLAG_PAR_WEIGHT_IS_FLEET_FIT)

    @property
    def tier3_stored(self):
        """bit9: this device has been through an intensity sweep."""
        return bool(self.flags & FLAG_TIER3_STORED)

    @property
    def par_provisional(self):
        """True unless BOTH calibration bits are confirmed.

        The firmware zones the flags word with opposite polarities so that an
        all-zero word - what a truncated or zeroed frame produces - reads as "no
        fault reported, nothing confirmed calibrated". Testing for confirmation
        therefore means requiring the bits to be SET; treating an unset bit as
        trustworthy is the exact failure the zoning exists to prevent.
        """
        return not (self.par_weight_is_fleet_fit and self.tier3_stored)

    @property
    def fatal_flags(self):
        """Flag bits that make this reading unusable as a calibration point."""
        return self.flags & FATAL_FLAGS

    def basic_counts(self):
        return basic_counts(self.raw, self.gain_low, self.gain_high,
                            self.atime, self.astep)

    def masked_channels(self, mask):
        """Channel names set in a 10-bit mask."""
        return [name for i, name in enumerate(CHANNELS) if mask & (1 << i)]

    def flag_names(self):
        return [text for bit, text in sorted(FLAG_NAMES.items()) if self.flags & bit]

    def to_dict(self):
        return {
            "format": self.format,
            "atime": self.atime, "astep": self.astep,
            "gain_low": self.gain_low, "gain_high": self.gain_high,
            "gain_low_x": gain_multiplier(self.gain_low),
            "gain_high_x": gain_multiplier(self.gain_high),
            "tint_ms": self.tint_ms,
            "full_scale": self.full_scale,
            "flags": self.flags, "flag_names": self.flag_names(),
            "par_weight_is_fleet_fit": self.par_weight_is_fleet_fit,
            "tier3_stored": self.tier3_stored,
            "par_provisional": self.par_provisional,
            "sat_mask": self.sat_mask, "sat_channels": self.masked_channels(self.sat_mask),
            "clip_mask": self.clip_mask, "clip_channels": self.masked_channels(self.clip_mask),
            "raw": list(self.raw),
            "chan": list(self.chan),
            "par": self.par,
            "par_tier2": self.par_tier2,
            "channels": list(CHANNELS),
        }


# format, atime, gain_low, gain_high | astep, flags, sat_mask, clip_mask
# | raw[10] | chan[10] | par | par_tier2.   '<' means no padding, and the layout
# is naturally aligned anyway (plan section 5), so the two agree byte for byte.
_SPEC_RAW_STRUCT = struct.Struct("<BBBB HHHH 10H 10f f f".replace(" ", ""))
assert _SPEC_RAW_STRUCT.size == SPEC_RAW_SIZE, _SPEC_RAW_STRUCT.size


def decode_spec_raw(payload):
    """Decode an 80-byte cmd-35 payload.

    :param payload: the bytes between ``0xA1`` and ``0xF0``
    :raises ValueError: on a wrong length or an unknown ``format``. A 32-byte
        payload means pre-rewrite firmware; say so rather than mis-parsing it.
    """
    if len(payload) == 32:
        raise ValueError(
            "cmd 35 returned the legacy 32-byte payload: this firmware predates "
            "the three-tier rewrite and has no par_tier2 to calibrate against")
    if len(payload) != SPEC_RAW_SIZE:
        raise ValueError(f"cmd 35 payload must be {SPEC_RAW_SIZE} bytes, got {len(payload)}")

    fields = _SPEC_RAW_STRUCT.unpack(payload)
    fmt, atime, gain_low, gain_high, astep, flags, sat_mask, clip_mask = fields[:8]
    raw = list(fields[8:18])
    chan = list(fields[18:28])
    par, par_tier2 = fields[28], fields[29]

    if fmt != SPEC_RAW_FORMAT:
        raise ValueError(f"cmd 35 format {fmt} is not the expected {SPEC_RAW_FORMAT}")

    return SpecRaw(format=fmt, atime=atime, gain_low=gain_low, gain_high=gain_high,
                   astep=astep, flags=flags, sat_mask=sat_mask, clip_mask=clip_mask,
                   raw=raw, chan=chan, par=par, par_tier2=par_tier2)


@dataclass
class SpecCal:
    """The device's five calibration vectors, read back via cmd 33/4 (section 6.4).

    These live *outside* ``ambit_calibration_info_t`` (decision 2), so they are
    NOT in the reboot dump. A host that reads only the boot banner sees a device
    that looks fully calibrated while its whole PAR chain is untouched. Always
    read this alongside the dump.
    """

    spec_offset: list
    spec_sens: list
    par_weight: list
    par_slope: float
    par_intercept: float
    format: int = SPEC_CAL_FORMAT
    source: str = "binary"          # "binary" (cmd 33/4) or "text" (get_spec_cal)

    def tier3(self):
        return (self.par_slope, self.par_intercept)

    def tier3_is_identity(self, rtol=1e-6):
        """True when tier 3 has never been written on this device.

        This - not ``flags`` bit2 - is the host's reliable test for "needs a
        tier-3 sweep". See read_par_provisional().
        """
        return (math.isclose(self.par_slope, TIER3_IDENTITY[0], rel_tol=rtol, abs_tol=1e-9)
                and math.isclose(self.par_intercept, TIER3_IDENTITY[1],
                                 rel_tol=rtol, abs_tol=1e-9))

    def seed_match(self, rtol=1e-4):
        """Which shipped vectors are still bit-for-bit the section 7 defaults.

        A mismatch is not an error - it means someone wrote a measured vector,
        which is the whole point of the campaign - but the tier-3 fit is only
        interpretable together with the tier-2 vector it sat on top of, so this
        goes in the record.
        """
        def same(got, want):
            return len(got) == len(want) and all(
                math.isclose(a, b, rel_tol=rtol, abs_tol=1e-12)
                for a, b in zip(got, want))
        return {
            "spec_offset": same(self.spec_offset, SEED_SPEC_OFFSET),
            "spec_sens":   same(self.spec_sens, SEED_SPEC_SENS),
            "par_weight":  same(self.par_weight, SEED_PAR_WEIGHT),
        }

    def par_weight_generation(self, rtol=1e-4):
        """Which generation of shipped ``par_weight`` this device carries.

        Deliberately a separate method rather than a fourth key in
        :meth:`seed_match`: that dict is three booleans and
        :func:`read_par_provisional` filters it by truthiness, so a string in
        there would read as a match.

        :return: :data:`SEED_GENERATION`, a key of
            :data:`SUPERSEDED_PAR_WEIGHTS`, or None for a vector that is neither -
            which means a fitted one, the outcome the campaign is for
        """
        def same(want):
            return len(self.par_weight) == len(want) and all(
                math.isclose(a, b, rel_tol=rtol, abs_tol=1e-12)
                for a, b in zip(self.par_weight, want))

        if same(SEED_PAR_WEIGHT):
            return SEED_GENERATION
        for generation, vector in SUPERSEDED_PAR_WEIGHTS.items():
            if same(vector):
                return generation
        return None

    def to_dict(self):
        return {
            "format": self.format,
            "source": self.source,
            "channels": list(CHANNELS),
            "spec_offset": list(self.spec_offset),
            "spec_sens": list(self.spec_sens),
            "par_weight": list(self.par_weight),
            "par_slope": self.par_slope,
            "par_intercept": self.par_intercept,
            "tier3_is_identity": self.tier3_is_identity(),
            "seed_match": self.seed_match(),
            "seed_generation": SEED_GENERATION,
            "par_weight_generation": self.par_weight_generation(),
        }


# format, reserved u8, reserved u16 | spec_offset[10] spec_sens[10]
# par_weight[10] | par_slope | par_intercept
_SPEC_CAL_STRUCT = struct.Struct("<BBH 30f f f".replace(" ", ""))
assert _SPEC_CAL_STRUCT.size == SPEC_CAL_SIZE, _SPEC_CAL_STRUCT.size


def decode_spec_cal(payload):
    """Decode a 132-byte cmd 33/4 payload."""
    if len(payload) != SPEC_CAL_SIZE:
        raise ValueError(f"cmd 33/4 payload must be {SPEC_CAL_SIZE} bytes, got {len(payload)}")
    fields = _SPEC_CAL_STRUCT.unpack(payload)
    fmt = fields[0]
    if fmt != SPEC_CAL_FORMAT:
        raise ValueError(f"cmd 33/4 format {fmt} is not the expected {SPEC_CAL_FORMAT}")
    vectors = fields[3:33]
    return SpecCal(spec_offset=list(vectors[0:10]),
                   spec_sens=list(vectors[10:20]),
                   par_weight=list(vectors[20:30]),
                   par_slope=fields[33], par_intercept=fields[34],
                   format=fmt, source="binary")


#: Labels the firmware's ``get_spec_cal`` prints, in the order it prints them.
#: Read off src/do_command.h - five lines, ``<label>:<comma-separated %.9g>``,
#: with the three vectors at ten floats each and the two tier-3 scalars alone.
SPEC_CAL_TEXT_FIELDS = (
    ("spec_offset", N_CHANNELS),
    ("spec_sens", N_CHANNELS),
    ("par_weight", N_CHANNELS),
    ("par_slope", 1),
    ("par_intercept", 1),
)


def parse_spec_cal_text(reply):
    """Parse the ``get_spec_cal`` text mirror of cmd 33/4.

    The firmware prints five labelled lines rather than one flat row::

        spec_offset:0.00196979,0.00724927,...
        spec_sens:34.9506626,65.2894843,...
        par_weight:40.8063026,42.1780116,...
        par_slope:1
        par_intercept:0

    Parsing keys on the labels rather than on position, so an extra line, a
    reordering, or interleaved log output cannot silently shift a vector by one
    field - which for these vectors would be undetectable in the numbers.

    :param reply: the whole reply, as one string or a sequence of lines
    :raises ValueError: if a label is missing or carries the wrong element count
    """
    if isinstance(reply, (list, tuple)):
        lines = [str(line) for line in reply]
    else:
        lines = str(reply).splitlines()                  # tolerates CR, LF and CRLF

    found = {}
    for line in lines:
        if ":" not in line:
            continue
        label, _, body = line.partition(":")
        label = label.strip().lstrip("[").strip()
        if label not in dict(SPEC_CAL_TEXT_FIELDS):
            continue
        values = []
        for token in body.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                values.append(float(token))
            except ValueError:
                break                       # trailing prose, stop at the numbers
        found[label] = values

    parsed = {}
    for label, count in SPEC_CAL_TEXT_FIELDS:
        values = found.get(label)
        if values is None:
            raise ValueError(f"get_spec_cal reply has no {label!r} line: {reply!r}")
        if len(values) != count:
            raise ValueError(f"get_spec_cal {label!r} must carry {count} float(s), "
                             f"got {len(values)}: {values!r}")
        parsed[label] = values if count > 1 else values[0]

    return SpecCal(spec_offset=parsed["spec_offset"],
                   spec_sens=parsed["spec_sens"],
                   par_weight=parsed["par_weight"],
                   par_slope=parsed["par_slope"],
                   par_intercept=parsed["par_intercept"],
                   source="text")


def usable_for_fit(spec_raw):
    """May this reading be used as a tier-3 sweep point?

    Rejects saturation, ASAT and acquisition faults for the obvious reason, and
    **rejects any dark-offset clip regardless of lamp drive** for a subtler one.

    The clip is the only nonlinearity in the whole chain (plan section 5), and
    tier 3 is fitted as a straight line, so a clipped point is off-model by
    construction. It also breaks the specific argument that lets miniPar's
    ``par_weight`` be reused at all: section 7c reuses it because ambit's extra
    offset subtraction costs a *constant* ``SUM w_i * offset_i`` = 1.09 umol
    (2.40 under the superseded OLS seed), which tier 3's intercept absorbs
    exactly. That constancy holds only while no channel is clipped. Once ``s`` saturates at zero, the offset term stops being
    constant and the relation stops being affine.

    The practical consequence is sharp: a fully clipped dark reading has
    ``par_tier2`` identically 0 against a reference of ~0, so it sits on the line
    only if ``par_intercept`` is 0 - and including it drags the fitted intercept
    toward zero, away from the ~8 umol the plan measures. A dark point is worth
    *taking* (it is the cheapest check that the fixture is light-tight) but not
    worth *fitting* unless it came back unclipped.

    :return: ``(usable, reasons)``
    """
    reasons = [FLAG_NAMES[bit] for bit in (FLAG_SATURATED, FLAG_ASAT, FLAG_FAULT)
               if spec_raw.flags & bit]
    if spec_raw.clip_mask:
        reasons.append("clipped at the dark offset on "
                       f"{spec_raw.masked_channels(spec_raw.clip_mask)} - the clip is "
                       "the chain's only nonlinearity, so the point is off-model")
    return (not reasons), reasons


def read_par_provisional(spec_raw, spec_cal):
    """Is this device's PAR trustworthy? Firmware flags first, vectors as a check.

    The firmware answers this directly, and better than the plan's original
    single-bit design did. ``flags`` bit8 says the compiled-in ``par_weight`` is
    an ambit fleet fit rather than a borrowed miniPar seed; bit9 says tier 3 has
    been stored for this device. Both are **positive polarity** and live in the
    high byte, apart from the negative-polarity condition bits in the low byte,
    so an all-zero word - a truncated or zeroed frame - reads as "nothing
    confirmed" rather than "all clear".

    So the host test is: PAR is trustworthy only when **both** high bits are set.
    That is a real improvement on what this function used to have to do, which
    was reconstruct the answer from the read-back vectors because the single bit2
    was unreliable in both directions.

    The vector comparison is kept as a cross-check, not as the primary test. It
    catches two things the bits cannot: which *generation* of seed a fit sits on
    top of (bit8 is one bit, not a provenance record), and a firmware whose
    compile-time flag disagrees with the vectors actually in NVS.

    :return: ``{"provisional", "reasons", "flag_par_weight_is_fleet_fit",
        "flag_tier3_stored", "tier3_unset", "on_seeds", "flag_vector_agreement"}``
    """
    seeds = spec_cal.seed_match()
    # spec_offset is excluded: it is an ams silicon constant, a real calibration
    # rather than a seed, so matching it says nothing about PAR provenance.
    on_seeds = [name for name, matched in seeds.items()
                if matched and name != "spec_offset"]
    tier3_unset = spec_cal.tier3_is_identity()
    par_weight_generation = spec_cal.par_weight_generation()

    reasons = []
    if (par_weight_generation is not None
            and par_weight_generation != SEED_GENERATION):
        # Reported whatever the bits say. bit8 is one bit and cannot carry a
        # generation, so a device on last month's firmware is indistinguishable
        # from one on this month's by flags alone - and the tier-3 fit is only
        # interpretable against the tier-2 vector it sat on.
        reasons.append(f"par_weight is the superseded {par_weight_generation} "
                       f"seed, not {SEED_GENERATION} - reflash before trusting "
                       f"a tier-3 fit taken on it")
    flag_fleet = flag_tier3 = None
    if spec_raw is not None:
        flag_fleet = spec_raw.par_weight_is_fleet_fit
        flag_tier3 = spec_raw.tier3_stored
        if not flag_fleet:
            reasons.append("firmware flags bit8 clear: par_weight is a borrowed seed, "
                           "not an ambit fleet fit")
        if not flag_tier3:
            reasons.append("firmware flags bit9 clear: no tier-3 sweep stored on "
                           "this device")

    # Cross-check. A disagreement means the bits and NVS tell different stories,
    # which is worth surfacing loudly rather than silently preferring either.
    disagreement = []
    if flag_tier3 is not None and flag_tier3 == tier3_unset:
        disagreement.append(
            f"flags bit9 says tier3_stored={flag_tier3} but the read-back tier 3 is "
            f"{'identity' if tier3_unset else 'non-identity'}")
    if flag_fleet is True and "par_weight" in on_seeds:
        disagreement.append("flags bit8 claims an ambit fleet fit but par_weight is "
                            "still bit-for-bit the miniPar seed")
    reasons.extend(disagreement)

    if spec_raw is None:
        # No reading to consult, so fall back to what the vectors show.
        if tier3_unset:
            reasons.append("tier 3 is still identity (1.0, 0.0) - no sweep on this device")
        for name in on_seeds:
            reasons.append(f"{name} is still the {SEED_GENERATION} miniPar seed")

    return {
        "provisional": bool(reasons),
        "reasons": reasons,
        "flag_par_weight_is_fleet_fit": flag_fleet,
        "flag_tier3_stored": flag_tier3,
        "tier3_unset": tier3_unset,
        "on_seeds": on_seeds,
        "flag_vector_agreement": not disagreement,
        "seed_generation": SEED_GENERATION,
        "par_weight_generation": par_weight_generation,
    }


# ============================================================================
# Host-side recompute: verify the firmware rather than trust it
# ============================================================================

@dataclass
class Recomputed:
    x: list                 # tier 1, basic counts (ms convention)
    s: list                 # offset-corrected, clipped at 0
    chan: list              # goal A
    clip_mask: int
    sat_mask: int
    par_tier2: float
    par: float


def recompute(spec_raw, spec_cal):
    """Re-derive the whole chain from ``raw[]`` plus the read-back vectors.

    This is the property that makes cmd 35 auditable (plan section 5): the raw
    counts stay on the wire so a host can reproduce every derived field. It only
    holds *with* the section 6.4 read-back, which is why this takes both.
    """
    x = spec_raw.basic_counts()
    full_scale = spec_raw.full_scale

    s, chan, clip_mask, sat_mask = [], [], 0, 0
    for i, xi in enumerate(x):
        si = xi - spec_cal.spec_offset[i]
        if si <= 0.0:
            si = 0.0
            clip_mask |= 1 << i
        s.append(si)
        chan.append(si * spec_cal.spec_sens[i])
        if spec_raw.raw[i] >= full_scale:
            sat_mask |= 1 << i

    t2 = math.fsum(w * si for w, si in zip(spec_cal.par_weight, s))
    return Recomputed(x=x, s=s, chan=chan, clip_mask=clip_mask, sat_mask=sat_mask,
                      par_tier2=t2,
                      par=spec_cal.par_slope * t2 + spec_cal.par_intercept)


def verify_firmware_math(spec_raw, spec_cal, rtol=2e-3, atol=1e-4):
    """Compare the firmware's derived fields against a host recompute.

    This single check covers every footgun in plan section 8 at once - the ms
    tick, the gain ordinal, the per-bank divisor, and the NIR/Clear swap - all
    of which are otherwise invisible at the pinned exposure. Tolerances are
    loose because the device works in f32 and this module in f64; a real bug in
    any of the four is orders of magnitude, not parts per thousand.

    :return: ``{"passed", "mismatches", "max_rel_error", ...}``
    """
    got = recompute(spec_raw, spec_cal)
    mismatches = []

    def close(a, b):
        return math.isclose(a, b, rel_tol=rtol, abs_tol=atol)

    def rel(a, b):
        scale = max(abs(a), abs(b), atol)
        return abs(a - b) / scale

    errors = []
    for i, (device, host) in enumerate(zip(spec_raw.chan, got.chan)):
        errors.append(rel(device, host))
        if not close(device, host):
            mismatches.append(f"chan[{i}] ({CHANNELS[i]}): device {device:.6g} vs host {host:.6g}")

    for label, device, host in (("par_tier2", spec_raw.par_tier2, got.par_tier2),
                                ("par", spec_raw.par, got.par)):
        errors.append(rel(device, host))
        if not close(device, host):
            mismatches.append(f"{label}: device {device:.6g} vs host {host:.6g}")

    if spec_raw.clip_mask != got.clip_mask:
        mismatches.append(f"clip_mask: device 0x{spec_raw.clip_mask:03x} vs host 0x{got.clip_mask:03x}")
    if spec_raw.sat_mask != got.sat_mask:
        mismatches.append(f"sat_mask: device 0x{spec_raw.sat_mask:03x} vs host 0x{got.sat_mask:03x}")

    return {
        "passed": not mismatches,
        "mismatches": mismatches,
        "max_rel_error": max(errors) if errors else 0.0,
        "rtol": rtol, "atol": atol,
        "host_par_tier2": got.par_tier2,
        "host_par": got.par,
        "host_basic_counts": got.x,
    }


# ============================================================================
# Tier 3: the affine fit and its quality gate
# ============================================================================

MIN_R2 = 0.995
MAX_NRMSE = 0.03
MAX_FULL_SCALE_RESIDUAL = 0.06
MAX_MONOTONIC_REVERSAL = 0.02
MIN_SWEEP_POINTS = 4

#: Tier 3 corrects ambit's optical stack against miniPar's, so a slope far from
#: 1.0 is legal but interesting. Outside this band the record gets a note; it is
#: not a rejection, because nobody has measured ambit optics yet.
EXPECTED_SLOPE_BAND = (0.2, 5.0)


def valid_par_slope(value):
    """Mirror of the firmware predicate: finite, ``(0, 100]``."""
    low, high = PAR_SLOPE_RANGE
    return isinstance(value, (int, float)) and math.isfinite(value) and low < value <= high


def valid_par_intercept(value):
    """Mirror of the firmware predicate: finite, ``|b| <= 500``."""
    return (isinstance(value, (int, float)) and math.isfinite(value)
            and abs(value) <= PAR_INTERCEPT_ABSMAX)


def assess_affine_fit(x_values, y_values, stimulus, *,
                      slope_min=None, slope_max=None, intercept_abs_max=None,
                      min_points=MIN_SWEEP_POINTS):
    """Fit ``y = par_slope * x + par_intercept`` and return a fail-closed record.

    Tier 3 is a **two-parameter** model and the intercept is a deliverable, not
    an error signal, so this is not the repo's ``assess_origin_fit`` with an
    extra output. The differences that matter:

    - The intercept is fitted and bounds-checked, never used as grounds for
      rejection. Forcing the line through the origin would fold a genuine
      additive offset into the slope, turning a constant error into a
      proportional one - worst exactly at low light, where a canopy or shade
      measurement lives. Tier 3's intercept absorbs two additive terms: the
      dark-offset term ``SUM w_i * offset_i``, which is 1.09 umol on the current
      seed, and miniPar's discarded tier-2 ``b0``. Under the superseded OLS seed
      those were 2.40 and 5.61 umol and the plan measured their sum directly
      (refitting ambit's chain over miniPar's data returned ``a = 1.0000,
      b = 7.983``, section 7c). That total cannot be restated for the current
      seed: ``par_coeffs_fleet.json`` does not export the ``b0`` its constrained
      fit discarded, so only the 1.09 umol half is known here. Which is a reason
      to fit the intercept, not to predict it.
    - Residuals, R^2 and NRMSE are scored against the affine prediction, so the
      R^2 gate tests linearity rather than whether the intercept happens to be
      small.
    - Negative inputs are permitted. The seeded ``par_weight`` is negative on
      NIR (and, on the superseded OLS seed, on F3, F5 and F8 as well), so a
      legitimate dark reading can produce a slightly negative ``par_tier2``. One
      negative coefficient is enough for that, so constraining the bands did not
      remove the case. Rejecting negatives would fail the dark point that the
      intercept is fitted from.
    - The reference must still rise monotonically with the applied stimulus, and
      the sweep must span a real range: an affine fit through a clustered sweep
      trades slope against intercept freely.

    :param x_values: device ``par_tier2`` per sweep point
    :param y_values: reference PAR per sweep point
    :param stimulus: lamp drive per point, used only to order the sweep
    :return: dict with ``passed``, ``reasons``, ``par_slope``, ``par_intercept``
    """
    slope_min = PAR_SLOPE_RANGE[0] if slope_min is None else slope_min
    slope_max = PAR_SLOPE_RANGE[1] if slope_max is None else slope_max
    intercept_abs_max = (PAR_INTERCEPT_ABSMAX if intercept_abs_max is None
                         else intercept_abs_max)

    x = [float(v) for v in x_values]
    y = [float(v) for v in y_values]
    drive = [float(v) for v in stimulus]
    if not (len(x) == len(y) == len(drive)):
        raise ValueError("sweep vectors must be equal length")

    reasons = []
    notes = []
    if len(x) < min_points:
        reasons.append(f"at least {min_points} sweep points are required, got {len(x)}")
    finite = all(math.isfinite(v) for v in x + y + drive)
    if not finite:
        reasons.append("all sweep values must be finite")
    if any(v < 0 for v in y):
        reasons.append("reference PAR must be non-negative")

    x_span = (max(x) - min(x)) if finite and x else 0.0
    y_span = (max(y) - min(y)) if finite and y else 0.0
    if x_span <= 0 or y_span <= 0:
        reasons.append("sweep must span a non-zero device and reference range")

    if finite and len(x) >= 2:
        order = sorted(range(len(drive)), key=drive.__getitem__)
        ox = [x[i] for i in order]
        oy = [y[i] for i in order]
        if any(b - a < -MAX_MONOTONIC_REVERSAL * x_span for a, b in zip(ox, ox[1:])):
            reasons.append("device par_tier2 is not monotonic with the applied stimulus")
        if any(b - a < -MAX_MONOTONIC_REVERSAL * y_span for a, b in zip(oy, oy[1:])):
            reasons.append("reference PAR is not monotonic with the applied stimulus")

    n = len(x)
    if finite and n >= 2 and x_span > 0:
        x_mean, y_mean = sum(x) / n, sum(y) / n
        sxx = sum((v - x_mean) ** 2 for v in x)
        sxy = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
        slope = sxy / sxx if sxx > 0 else math.nan
        intercept = y_mean - slope * x_mean if math.isfinite(slope) else math.nan
    else:
        x_mean = y_mean = slope = intercept = math.nan

    if math.isfinite(slope) and math.isfinite(intercept):
        prediction = [slope * v + intercept for v in x]
        residual = [a - b for a, b in zip(y, prediction)]
        ss_res = math.fsum(v * v for v in residual)
        ss_tot = math.fsum((v - y_mean) ** 2 for v in y)
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else math.nan
        nrmse = math.sqrt(ss_res / n) / y_span if y_span > 0 else math.inf
        max_residual_fraction = max(abs(v) for v in residual) / y_span if y_span > 0 else math.inf
    else:
        prediction, residual = [], []
        r2, nrmse, max_residual_fraction = math.nan, math.inf, math.inf

    # The intercept only matters relative to the light levels it will correct,
    # so report it against the dimmest lit point rather than against full scale.
    lit = [(a, b) for a, b in zip(drive, y) if a > 0]
    dimmest = min((b for _a, b in lit), default=math.nan)
    intercept_vs_dimmest = (abs(intercept) / dimmest
                            if math.isfinite(intercept) and dimmest and math.isfinite(dimmest)
                            else math.nan)
    if math.isfinite(intercept_vs_dimmest) and intercept_vs_dimmest > 0.25:
        notes.append(f"intercept is {intercept_vs_dimmest:.0%} of the dimmest lit reference "
                     f"point - extend the sweep downwards to constrain it")

    # Dark point: with a fitted intercept, PAR at zero drive should reproduce it.
    dark = [(a, b, c) for a, b, c in zip(drive, x, y) if a == 0]
    dark_residual = math.nan
    if dark and math.isfinite(slope):
        _d, dx, dy = dark[0]
        dark_residual = dy - (slope * dx + intercept)
    else:
        notes.append("no zero-drive point in the sweep - the intercept is extrapolated")

    if not valid_par_slope(slope) or not slope_min < slope <= slope_max:
        reasons.append(f"par_slope must be finite and within ({slope_min}, {slope_max}]")
    if not valid_par_intercept(intercept) or abs(intercept) > intercept_abs_max:
        reasons.append(f"par_intercept must be finite and |b| <= {intercept_abs_max}")
    if not math.isfinite(r2) or r2 < MIN_R2:
        reasons.append(f"R-squared must be at least {MIN_R2}")
    if not math.isfinite(nrmse) or nrmse > MAX_NRMSE:
        reasons.append(f"normalized RMSE must be at most {MAX_NRMSE}")
    if not math.isfinite(max_residual_fraction) or max_residual_fraction > MAX_FULL_SCALE_RESIDUAL:
        reasons.append(f"maximum residual must be at most {MAX_FULL_SCALE_RESIDUAL} of full scale")

    if math.isfinite(slope) and not EXPECTED_SLOPE_BAND[0] <= slope <= EXPECTED_SLOPE_BAND[1]:
        notes.append(f"par_slope {slope:.4g} is outside the expected "
                     f"{EXPECTED_SLOPE_BAND} band - ambit optics differ from the "
                     f"miniPar stack the seed par_weight was fitted on by more "
                     f"than that, or the reference is misconfigured")

    return {
        "passed": not reasons,
        "reasons": reasons,
        "notes": notes,
        "fit": "affine",
        "par_slope": slope,
        "par_intercept": intercept,
        "r2": r2,
        "nrmse": nrmse,
        "max_residual_fraction": max_residual_fraction,
        "residual": residual,
        "prediction": prediction,
        "dark_residual": dark_residual,
        "intercept_vs_dimmest_lit": intercept_vs_dimmest,
        "n_points": n,
        "x_span": x_span,
        "y_span": y_span,
        "thresholds": {
            "slope_min": slope_min, "slope_max": slope_max,
            "intercept_abs_max": intercept_abs_max,
            "min_r2": MIN_R2, "max_nrmse": MAX_NRMSE,
            "max_full_scale_residual": MAX_FULL_SCALE_RESIDUAL,
            "max_monotonic_reversal": MAX_MONOTONIC_REVERSAL,
            "min_points": min_points,
        },
    }


# ============================================================================
# Reference-transfer bookkeeping
# ============================================================================

# The assessed cost of using a MiniPAR rather than a Li-Cor Li-250A used to live
# here as REFERENCE_TRANSFER_NOTE and was copied verbatim into every calibration
# record (twice - once under the fit, once under the reference). It was the same
# fixed prose in every payload, so it is documentation, not data: the argument and
# its figures are in the README under "The MiniPAR as reference instead of a
# Li-250A". What each record still carries is what is actually per-run and what a
# later Li-250A cross-check needs - the reference's identity, its settings, its
# own slope/intercept, and `par_tier2` per sweep point.


def spectral_drift(spectra):
    """How much the lamp's spectrum moved across the sweep.

    A halogen lamp driven from 0.8 A to 6.6 A shifts colour temperature by
    hundreds of kelvin, so an intensity sweep is not a single spectrum. That is
    mostly harmless here - both instruments see the same light and use the same
    tier-2 weights, so it cancels - but it belongs in the record, because it is
    the term that limits how far the fitted slope can be trusted away from the
    calibration spectrum.

    :param spectra: per-point sequences of channel values (any consistent units)
    :return: ``{"max_shape_deviation", "per_channel_span", ...}`` or None
    """
    usable = [list(map(float, s)) for s in spectra
              if s and len(s) == N_CHANNELS and sum(abs(v) for v in s) > 0]
    if len(usable) < 2:
        return None

    shapes = []
    for values in usable:
        total = math.fsum(values)
        if total == 0:
            continue
        shapes.append([v / total for v in values])
    if len(shapes) < 2:
        return None

    mean_shape = [math.fsum(col) / len(shapes) for col in zip(*shapes)]
    per_channel_span = [max(col) - min(col) for col in zip(*shapes)]
    deviation = max(max(abs(s[i] - mean_shape[i]) for i in range(N_CHANNELS))
                    for s in shapes)
    return {
        "n_spectra": len(shapes),
        "channels": list(CHANNELS),
        "mean_shape": mean_shape,
        "per_channel_span": per_channel_span,
        "max_shape_deviation": deviation,
        "note": "fraction-of-total per channel; a large span means the sweep is a "
                "spectral curve, not a pure intensity ray, so the fitted slope is "
                "specific to this lamp",
    }
