# new_Calibratron

The Calibratron rewritten for the ambit cmd-35 three-tier AS7341 chain
(`ambit/plans/AMBIT_COMMAND35_SPECPAR.md`). Self-contained: it does not import
the repo-root `helpers.py`.

```
spec_cal.py          pure - cmd-35 codecs, tier math, the tier-3 affine gate
quality.py           pure - the LED origin gate and the ADPD baseline gate
helpers.py           transport - discovery, Ambit link, references, record
run_calibratron.py   the bench sequence
tests/               78 tests, no hardware needed
```

Nothing here imports the repo-root `helpers.py` or `calibration_quality.py`.

Run: `python run_calibratron.py`  ·  Test: `python -m pytest tests -q`

## What it calibrates on the PAR chain

**`par_slope` and `par_intercept`. Nothing else.**

| vector | source | this script |
|---|---|---|
| `spec_offset[10]` | ams workbook — device-independent | reads back, verifies |
| `spec_sens[10]` | miniPar LR1-B seed (plan §7b) | reads back, records |
| `par_weight[10]` | miniPar Li-250A fleet seed, constrained fit (plan §7c) | reads back, records |
| **`par_slope`** | **per-device intensity sweep** | **fits, writes, verifies** |
| **`par_intercept`** | **same sweep** | **fits, writes, verifies** |

The three seeded vectors ship as firmware defaults. There is no code path here
that writes them, deliberately rather than by omission: a per-device tier-2 fit
from a single-lamp sweep scores R² −31 … −7443 with 434–779 % median error
(plan §2). An intensity sweep moves the sample along one ray in 10-space, so it
identifies a scale, not ten weights.

## What was dropped from the old Calibratron

| dropped | why |
|---|---|
| cmd 31 / `get_par` / `PAR` — the whole legacy PAR path | Integer `Spec_COE` weights on un-normalised counts at a pinned exposure, packed into a uint16 that wraps above ~11 % of full scale. A bright reading came back looking dark. |
| `set_par_gain` / `set_spec` writes | `spec_coef` is frozen (plan decision 7). Still **read** and recorded — cmd 31 and deployed devices depend on it — never written. |
| `ambit_spec_unscale`, `ambit_par_minipar_method`, `AMBIT_PAR_DISCREPANCIES` | All three existed to measure the gap between the legacy weighting and the MiniPAR's. Ambit now *ships* miniPar's fleet vector, so the gap they characterised is gone. |
| `basic_count_divisor` (seconds tick) | Replaced by `spec_cal.integration_time_ms`. The two differ by 1000×; keeping both invites picking the wrong one. |
| Origin-forced gating of the PAR fit | See below. |
| the repo-root `calibration_quality.py` | Replaced by `quality.py`, which fixes four things that were wrong for this bench — see below. The root module stays for the legacy script. |

Kept unchanged in substance: firmware flashing via `firmware_fetch`, device
naming, actinic-LED gain, ADPD dark baseline, openJII publish.

## The ADPD photodiodes during both sweeps

`RECORD_ADPD_TRACES = True` records an `arrun2` trace — **leaf, sun**, s_630,
r_630, env — at every point of *both* sweeps, and prints a per-point table.
Nothing is fitted or written from these; they go in the record for later
analysis.

The tier-3 sweep is the valuable one: it pairs the photodiodes with a
**calibrated PAR reference across the whole lamp range**, which is a PAR response
curve for `leaf` and `sun` obtained for free, at `actinic = 0` so they see the
same light the reference does. The LED sweep records them at `actinic = setting`,
where the Ambit's own LED is the source.

Three things the implementation has to get right:

- **The ADPD pulse LEDs are zeroed before every trace.** Without
  `set_currents,0,0,0` the detector sees its own 620/720/IR pulses and `sun` /
  `leaf` stop being a measurement of the incident light. Re-asserted per trace
  rather than once per sweep: it costs one write, and if anything reset the device
  mid-sweep the currents would come back non-zero and every later reading would be
  contaminated with nothing in the record saying so.
- **The trace comes last at each point.** It zeroes the pulse LEDs and drives the
  actinic, both of which change what the AS7341 sees, so it must not precede the
  cmd-35 read it is paired with.
- **Saturation is detected per channel.** A pinned photodiode is the failure that
  quietly ruins a later analysis — the samples stay plausible and the spread
  collapses to zero. `summarize_arrun` flags any channel within 1 % of the 24-bit
  ceiling, the table marks it `!`, and `assess_adpd_sweep` names the drive it
  pinned at. It also reports a dead or reversed channel. All **reported, never a
  gate**: a flat photodiode must not discard an otherwise good tier-3 fit.

Cost is about 0.5 s of trace plus overhead per point, ~14 s across both sweeps.
Cheaper than the old script's version, which opened and closed the port per point
and so paid a device reset and boot each time. `s_730` / `r_730` stay empty
because the run is type 2 (no IR reflect); `sun` and `leaf` are populated only
because ambient sub-sampling is 1.

## Why the PAR fit needs its own quality gate

`calibration_quality.assess_origin_fit` fits `y = k·x` and uses a free-intercept
fit as a *failure detector* — an intercept above 5 % of full scale means the
origin model was wrong, so it rejects. Tier 3's intercept is a **deliverable**,
so that logic inverts: the thing being measured becomes grounds for rejection.

The deeper cost is what origin-forcing does to the numbers. With a true
`b = 7.983` — the value plan §7c measured on the superseded OLS seed, 2.40 µmol
of dark-offset term plus 5.61 µmol of miniPar's discarded tier-2 intercept —
forcing through the origin folds the offset into the slope. At the bright end that is a sub-1 % error; at
120 tier-2 counts it is over 5 %, and it keeps growing as the light drops. **A
constant additive error becomes a proportional one, worst exactly where canopy
and shade measurements live.** `tests/test_spec_cal.py::test_origin_forcing_would_misplace_a_real_intercept`
pins that.

Two further incompatibilities: R² scored against the origin-forced prediction
gates on "did `b` happen to be small" rather than on linearity; and the
non-negativity check rejects a legitimately slightly-negative `par_tier2` at the
dark point, which the seeded `par_weight` (negative on NIR) can
produce. That is the one point the intercept is fitted from.

So `spec_cal.assess_affine_fit` fits both parameters, bounds-checks them against
the firmware predicates (`par_slope` ∈ (0,100], |`par_intercept`| ≤ 500), scores
R²/NRMSE against the affine prediction, permits negative inputs, and demotes the
intercept-size question to a *note* keyed on the dimmest lit point rather than a
rejection.

## What `quality.py` fixes in the root `calibration_quality.py`

The root module's arithmetic is fine and the legacy script still uses it. Four
things were wrong for the new approach, and `quality.py` is a straight
replacement rather than a wrapper because two of them are API shape:

1. **Its low coefficient bound was inclusive where the firmware's is exclusive.**
   `valid_actinic_coefficient` is `> 0.01 && <= 1.0`, and ambit's own test
   asserts *"actinic lower bound is exclusive"*. The host gate was
   `coefficient_min <= coefficient`, so a fit of exactly `0.01` **passed host QC
   and was then refused by the device** — the bench would report a successful
   write with nothing changed. Same class as sending an unrounded `par_slope`.
2. **One gate was dead at the only call site.** The old signature was
   `(x, y, stimulus)` and `calibrate_led` passed the LED settings as *both* `y`
   and `stimulus`, so *"reference readings are not monotonic with the applied
   stimulus"* compared the settings to themselves and could never fire.
   `assess_led_fit(measured, settings)` takes two arguments, which makes the
   mistake impossible instead of merely unlikely.
3. **A slightly negative dark reading failed the whole sweep closed.** Any
   negative value tripped *"calibration values must be non-negative"*. The
   MiniPAR reports `par_raw * slope + intercept`; with a negative intercept a
   genuinely dark reading comes back a shade under zero. That is noise. Small
   negatives are now clamped with a note; large ones still fail.
4. **Its thresholds shared names with different values in `spec_cal`.** Both
   defined `MIN_R2`, `MAX_NRMSE` and `MAX_MONOTONIC_REVERSAL`, at 0.99/0.05/0.02
   against 0.995/0.03/0.02. Two places to get a tuning edit wrong. Every
   threshold here is prefixed `LED_*` or `ADPD_*`, and a test asserts the names
   cannot collide.

The ADPD baseline gate moved out of the runner into the same file, so every
numerical gate the bench applies now sits next to the firmware bound it mirrors.

The origin fit itself is **kept, and is correct here**: zero drive is zero light
with no offset to absorb, so a free intercept is evidence against the model
rather than a quantity to fit — the opposite of tier 3.

One honest caveat on what the LED step buys you: as of `ambit@fix/spec_overflow`,
`actinic_coef` is validated, persisted and printed but **never applied** —
`AS_LED_Current()` takes the requested setting directly. So the step
characterises the LED rather than changing device behaviour. Worth running, worth
not over-trusting. Noted in the function's docstring too.

## The MiniPAR as reference instead of a Li-250A

Accepted. The cost is smaller than it looks, for a structural reason.

Ambit's seeded `par_weight` **is** miniPar's fleet vector, so at any single
calibration spectrum both instruments compute `w · s` over the same weights.
Whatever spectral bias `w` carries at that spectrum is common-mode and divides
out of the ratio, leaving the fitted slope as (near enough) the pure
optical-throughput ratio of the two stacks. The MiniPAR behaves as a **transfer
standard**, and the fit's *linearity* is untouched — which is why the QC gates
still mean what they say.

What it costs is a pure multiplicative bias: the reference MiniPAR's own PAR
error at the calibration lamp's spectrum.

| term | figure | source |
|---|---|---|
| MiniPAR in-domain | 1.96 % median, R² 0.9983 | plan §7c, 462 samples, 3.7–2033.7 µmol |
| MiniPAR cross-source | **4.37 % median, 9.82 % worst device** | plan §2, LED-calibrated → scored on daylight |
| Li-250A absolute accuracy | ~5 % | Li-Cor spec, unavoidable either way |
| **combined** | **~7 % typical, ~10 % worst** | in quadrature |

The cross-source row is the honest one: the bench lamp is halogen, which is not
in miniPar's daylight-dominated calibration set. So the choice takes you from
~5 % to ~7 %.

**It is recoverable without redoing bench work.** The record stores `par_tier2`
per sweep point, so one later Li-250A comparison on a single device yields a
scale correction applicable to every stored sweep. That is why the payload
carries the whole sweep and not just the fitted pair.

Two caveats the record captures rather than hides:

- **The sweep is not one spectrum.** A halogen lamp from 0.4 A to 6.6 A shifts
  colour temperature by hundreds of kelvin. Both instruments see it and use the
  same weights, so it largely cancels — but `spec_cal.spectral_drift` measures how
  far it moved, because that bounds how far the fitted slope travels from this
  lamp.
- **`par_slope` far from 1.0 is informative.** `par_tier2` is already in µmol
  (plan §7c fitted `w` to produce them directly, and a tier-3 refit on miniPar
  data returns `a = 1.0000`). A slope outside 0.2–5.0 means ambit's optics differ
  from miniPar's by more than that, or the reference is misconfigured. Noted, not
  rejected — nobody has measured ambit optics yet.

## A clipped point cannot be fitted — including the dark one

Found while checking the plan against the sweep design; now written into plan §7c.

Plan §7c justifies reusing miniPar's `par_weight` at all by showing that ambit's
extra offset subtraction costs a *constant* `Σ wᵢ·offsetᵢ` — 1.09 µmol on the
current seed, 2.40 on the superseded OLS one — which tier 3's intercept absorbs
exactly. **That constancy holds only while no channel
is clipped.** Once `s` saturates at zero the offset term stops being constant and
the relation stops being affine — and the clip is the chain's only nonlinearity
(§5).

This became live rather than theoretical when the real ams offsets replaced
zeros: with a zero offset vector `s == x` always and nothing can ever clip. The
offsets are only **0.28 – 2.02 raw counts** at the pinned 2× / 139 ms, so the
clip bites solely in a light-tight fixture — but there it bites on all ten
channels at once, which is exactly the dark point of a sweep.

Concretely: a fully clipped dark reading has `par_tier2` identically 0 against a
reference of ~0, so it lies on the line only if `par_intercept` is 0. Folding it
into the fit drags the intercept from 7.983 to 4.78 — **40 % low — while R² stays
above 0.99, so no quality gate catches it.**
(`tests/…::test_fitting_a_clipped_dark_point_would_bias_the_intercept`.)

So `spec_cal.usable_for_fit` rejects any clipped point at any lamp drive, and the
sweep gained a 0.4 A step: 0.0 A stays as the cheapest light-tightness check, and
0.4 A is a genuine low anchor inside the linear regime. If the dark point comes
back unclipped it is fitted too — the rule keys on `clip_mask`, not on the drive.

## The `par_weight` seed has a generation, and it changed

`SEED_PAR_WEIGHT` is the **constrained** miniPar fleet fit — `lsq_linear` on
basic counts with `F1–F8 ≥ 0` and Clear/NIR free, shrunk toward `W_TARGET` at
λ=0.03, exported by `miniPar/new_calibration_miniPAR/par_coeffs_fleet.json`.
`SEED_GENERATION` is `minipar-2026-08-18-constrained`.

It replaced a plain OLS fit on the same 462 samples. That one predicted in-domain
PAR just as well (in-sample R² 0.9984 against 0.9982) but its signs were not
physical — negative on F3, F5 and F8, positive on Clear — because the design
matrix has condition number ≈ 451 on a daylight-dominated set. The practical cost
is leverage on spectral shape rather than in-domain error: L1 norm 1172 against
457 for a 1.8× larger net response, and off-daylight it shows up directly —
canopy PAR spread 6.2 % against 0.4 %, coefficient direction spread p95 62° against 8°.

Two things follow for this script:

- **A tier-3 fit is only interpretable against the tier-2 vector it sat on.** The
  two vectors give tier-2 values within 5–8 % of each other on the bench lamp, so
  a re-swept `par_slope` moves by about that much and no gate would notice. Every
  record therefore carries `seed_generation` *and* `par_weight_generation`.
- **A device on older firmware must be named, not called unknown.** A `False` in
  `seed_match` means "someone wrote a fitted vector here" everywhere else, so
  `SUPERSEDED_PAR_WEIGHTS` keeps the previous vector and
  `SpecCal.par_weight_generation()` reports `minipar-2026-08-17-ols` for it.
  `read_par_provisional` adds a reason saying to reflash before trusting a fit
  taken on it.

`spec_sens` and `spec_offset` were already bit-for-bit the current
`spectral_coeffs_fleet.json` export and did not change. `bit8` stays clear: this
is still a fit on miniPar optics, not an ambit one. And the export still carries
`prior_is_placeholder: true`, so expect one more generation once the CM weights
are computed.

## How this host reads the flags word

The firmware zones `flags` into two bytes of **opposite polarity**, and it is
worth understanding before writing any host code against it:

| | | |
|---|---|---|
| low byte | conditions, `1` = needs attention | bit0 full scale · bit1 dark-offset clip · bit2 ASAT · bit3 acquisition fault |
| high byte | calibration, `1` = **confirmed** | bit8 `par_weight` is an ambit fleet fit · bit9 tier 3 stored for this device |

An all-zero word — what a truncated or zeroed frame produces — therefore reads
as "no fault reported, nothing confirmed calibrated", which is the pessimistic
reading. Confirmation requires bits to be **set**; treating an unset bit as
trustworthy is the failure the zoning prevents. So `par_provisional` here is
`not (bit8 and bit9)`, and `spec_cal.py` never collapses the two.

Two consequences for this script:

- **bit8 stays clear after a successful calibration**, because
  `AMBIT_PAR_WEIGHT_IS_AMBIT_FIT` is compile-time `false` until the ambit
  Li-250A campaign lands. Every device the Calibratron produces will report
  provisional PAR. That is correct, and it is never a success criterion here —
  the criteria are the cmd 33/4 read-back and the confirmation pass.
- **bit9 is authoritative for "has this device been swept"**, keyed on NVS key
  presence. `read_par_provisional` still cross-checks it against the read-back
  vectors, but the check is **directional**: bit9 latches for the life of the
  NVS partition (there is no verb to delete the key — firmware plan §5a), so
  bit9 set over identity vectors is a legal state (a bench write, or a slope
  restored to exactly 1.0) that earns a printed reason and nothing more. Only
  the impossible direction — bit9 clear over non-identity vectors — **aborts
  the sweep**, because a fit taken on top of an unknown tier-2 state is not a
  calibration.

**The reboot dump does not contain the PAR chain.** The five vectors live outside
`ambit_calibration_info_t` by design (decision 2), so the boot banner shows a
device that looks fully calibrated while its PAR chain is untouched. Every step
here reads cmd 33/4 alongside the dump, and read-back verification goes through
cmd 33/4, never through `ambit_reboot`.

**Writes have three outcomes, not two.** `report_spec_save` answers
`"<what> saved and verified"`, `"<what> rejected"`, or
`"<what> save failed: <ESP_ERR_…>"`. The third contains no negative keyword, so a
host testing for `"rejected"` reads an NVS failure as a success. Acceptance is
tested **positively** on `saved and verified` — one test that covers all three
outcomes and any future rewording.

**`get_spec_cal` is five labelled lines**, not one CSV row:
`spec_offset:` / `spec_sens:` / `par_weight:` (ten `%.9g` each) then `par_slope:`
and `par_intercept:`. `parse_spec_cal_text` keys on the labels, never on
position — a one-slot shift in these vectors is undetectable in the numbers,
since every element is a plausible magnitude for its neighbour. `%.9g`
round-trips an IEEE-754 float exactly, so the text mirror and cmd 33/4 agree bit
for bit.

## Bench sequence

```
probe cmd 35 (80 B, format 1)          -> abort tier 3 if absent; no legacy fallback
read cmd 33/4                          -> previous tier 3, for rollback
verify firmware math from raw[]        -> catches tick / gain ordinal / bank / NIR-Clear
sweep 0.0 .. 6.6 A                     -> par_tier2 vs reference PAR, per-point masks
affine fit + QC
write par_slope, par_icept             -> verify via cmd 33/4, roll back on mismatch
confirm at 0.8 / 3.0 / 6.6 A           -> device par vs reference, closed loop
```

The firmware-math check is the one that earns cmd 35's design: `raw[]` stays on
the wire so the host can reproduce every derived field, and a single comparison
covers all four footguns in plan §8 — each of which is otherwise invisible at the
pinned exposure. If it fails, the run stops before the fit, because a fit taken
on wrong arithmetic would encode the bug as a calibration coefficient.

## Every MiniPAR read is a coin flip unless the port is opened cold

The MiniPARs are ESP32-class boards with reset wired to DTR/RTS, so pyserial's
default open — which asserts both — reboots the instrument. The command then goes
into a booting device and the reply is one of:

```
Saved PC:0x40053b60          bootloader output
load:0x3fcd5810,len:0x438    bootloader output
error:unknown_command        the command line, chewed up by the reset
17.056                       the previous question's answer, still buffered
```

Only the last one is dangerous: it parses. A stale `par` reading pairs the wrong
reference with the Ambit's `par_tier2`, and the fit absorbs it silently.

So `helpers._query` opens cold (`open_serial_no_reset`, DTR/RTS set *before*
open), drops whatever is already buffered, skips bootloader lines without
spending an attempt, and re-asks up to `QUERY_ATTEMPTS` times whenever the reply
is an `error:` or the wrong *shape* for the question — every getter passes what
shape it expects. `_command` is deliberately left on the default open: its only
user is the Kiprim DC source, which has no reset on those pins and may hold its
output until DTR is asserted.

A reference that still will not answer raises `ReferenceUnavailable` rather than
returning a number: the tier-3 sweep aborts with `status:
reference_unavailable`, writes nothing, and drops the lamp to 0 A in a `finally`.
A partial sweep against a silent reference is not a calibration.

## BAD COMMAND: one space in a command template

`set_currents` is implemented in every firmware this bench flashes
(`ambit/src/do_command.h`, `case hash("set_currents")`, printing `Currents set to
%d, %d, %d`). Six `BAD COMMAND` replies to it per run were not a missing verb.
They came from the **trailing space** in `arrun1` / `arrun2`'s padding, `",
, 
"`.

The device reads each command with `Serial_Input_Chars(choose, ":,", 200, ...)`, and
`serial.cpp` gives that reader three relevant properties:

- `:` and `,` end a token and are discarded;
- CR/LF are skipped **without being stored** and without restarting the timer;
- anything else is stored, and storing restarts a **200 ms** inter-character window.

A space is printable, so it is stored as token byte 0 and opens that window.
Whatever the host sends inside it joins the same token, so `set_currents,0,0,0,`
arrives as `" set_currents"` - and `do_command()` drops any token whose first
character fails `isalnum()`, silently. The verb is gone. Its three `0` arguments
are then read as commands, and a digit-leading token goes through `atoi` into a
switch with no numeric cases: `BAD COMMAND`, three times.

Why the first trace of each sweep always worked: `set_actinic` sleeps 0.3 s and
flushes, which outlasts the 200 ms window. `arrun` returned the instant it saw
`Data sent` and did not - so traces 2..7 lost their `set_currents` and the ADPD
pulse LEDs stayed live through the rest of the sweep.

Fixed at the root (padding is now `",
,
"`, which stores nothing) and defended
at the host (`AmbitLink._settle_console`, `CONSOLE_TOKEN_SETTLE_S = 0.25`, after
every trace). `AmbitLink.text` also takes `expect=` now, which was a real fix for
stale-line attribution but was NOT this bug - there was no acknowledgement to
find, because the verb never reached the parser.

`tests/test_console_tokenizer.py` models the reader's three rules and asserts the
next verb survives each padded template; it also asserts the old padding fails,
so the guard is shown to catch what it was written for.

The consequence was not cosmetic: `pulse_currents_zeroed` was `False`, so `leaf`
and `sun` in those traces measured the Ambit's own pulse LEDs alongside the
incident light. That flag now travels beside `adpd_stats` (raw samples are
dropped, provenance is not), the station block reports
`pulse_leds_zeroed_confirmed` rather than asserting the intent, and one line under
the table tells the operator the numbers are contaminated.

## `leaf` and `sun` face different directions

Which photodiode responds is fixture geometry, not detector health:

| sweep | `leaf` | `sun` |
|---|---|---|
| halogen lamp, 0.0-6.6 A | span 69, wanders | **span 927, monotonic** |
| Ambit actinic LED, 10-250 | **790 -> 1134, monotonic** | flat at ~655 |

So `assess_adpd_sweep` takes `responders=` and only the channel aimed at the
source gets the flat / non-monotonic notes - `("sun",)` for the lamp sweep,
`("leaf",)` for the LED sweep. The other channel is still recorded, and
saturation is still reported for both: a pinned photodiode is a fault wherever it
points. Before this, every run reported the geometry as a fault, which is how a
genuinely flat channel would have gone unnoticed.

`env` is not a photodiode at all - it is the MLX object temperature, sampled once
per trace rather than once per pulse. Its `+-0.0` is arithmetic on a single value,
not an unusually quiet channel, and the object cannot warm measurably inside a
0.5 s trace.

## A missing instrument stops the bench

`REQUIRE_ALL_DEVICES` (default on). A role that does not answer used to degrade
quietly: tier 3 printed one `[skip]` line and the run still saved and uploaded a
record. Discovery failing is a bench fault, so it now aborts before the reboot
dump and before the rename prompt - nothing has been touched at that point.

Discovery also says *why* now. A port that is present but will not open used to
be a `logger.debug` line, which made it indistinguishable from an unplugged
instrument; both are warnings, and the "no device for role" message lists every
port that was actually asked.

## Status

The firmware side this host talks to is **built**, on `ambit@fix/spec_overflow`:
the cmd-35 payload, cmd 33/4, the five text setters, `get_spec_cal`, the
predicates, the two-zone flags word, and the seeded `spec_sens` / `par_weight`
in `nvs1.h` are all in place. Verified against the source, not assumed.

What is still open is measurement, not code (plan §11): no vector here is fitted
on ambit hardware. `AMBIT_PAR_WEIGHT_IS_AMBIT_FIT` is `false`, so bit8 stays
clear and every device this bench produces reports provisional PAR — as it
should, until an ambit Li-250A campaign replaces §7c.

## Desktop GUI

`calibratron_gui.py` is a tkinter front-end over `run_calibratron.py`: the same
per-device pass (flash, name, tier-3 PAR, actinic LED, record, publish), with
the bench instruments confirmed from the cached port roles for every device and
the openJII sign-in kept for the whole session.

Download the ZIP for your platform from this repository's GitHub Releases:

- `calibratron-windows-x64.zip`: extract the whole folder and run `calibratron.exe`.
- `calibratron-linux-x64.zip`: extract and run `calibratron/calibratron` (Ubuntu 22.04 or newer).
- `calibratron-macos-arm64.zip`: extract and open `calibratron.app` (Apple Silicon).

Keep the bundle's supporting files beside the executable. Python and esptool do
not need to be installed separately; the serial drivers and the bench hardware
are still required. The bundles are unsigned, so Windows and macOS may show an
unknown-publisher warning. Intel macOS is not packaged.

For a source checkout, install `requirements-gui.txt` (Linux also needs the
distribution's `python3-tk`), then run `python calibratron_gui.py`. Press
**Rescan bench**, select the steps, and press **Start calibration**.

Packaged applications keep `calibrations/`, `firmware_cache/` and the
`.port_roles.json` port hints beneath a per-user data directory
(`runtime_paths.data_dir()`):

| Platform | Data directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%/Calibratron` |
| macOS | `~/Library/Application Support/Calibratron` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/Calibratron` |

Set `CALIBRATRON_DATA_DIR` to override it. A source checkout uses the checkout
folder itself. Closing the GUI deletes the downloaded firmware cache, so every
session starts from the release that is current *now*; a locally selected
firmware folder is never touched. API-key settings live in the separate
per-user configuration directory managed by `openjii_auth.py`.

## GUI release pipeline

`.github/workflows/gui-release.yml` tests and builds Windows x64, Linux x64 and
macOS ARM64 bundles on pull requests, pushes to `main`, and manual runs. Every
platform runs the packaged executable's `--smoke-test`, which constructs the
GUI without signing in, loads the bundled CA roots, and exercises the bundled
esptool without touching a serial port. PR and manual runs leave the ZIPs as
Actions artifacts.

After all three builds pass on `main`, the same run's ZIPs and `SHA256SUMS` are
uploaded to a draft release and then published. Tags are `calibratron-vX.Y.Z`
and the patch number increments automatically; re-running an already published
commit does not create another release. Publication uses the built-in
`GITHUB_TOKEN`: no extra secret and no hand-made tag. The workflow builds the
merged `main` commit, so merge commits, squash merges and direct pushes all
release the same way.

Local rehearsal of what CI does:

```bash
python -m pip install -r requirements-build.txt
python -m pytest tests -q
pyinstaller --noconfirm --clean --onedir --windowed --name calibratron \
  --paths . --collect-all esptool packaging/launcher.py
dist/calibratron/calibratron --smoke-test     # Linux: xvfb-run -a ...
```

Automated packaging checks do not replace a physical bench test.
