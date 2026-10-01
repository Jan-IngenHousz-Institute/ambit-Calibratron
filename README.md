# AMBIT Calibratron

Calibratron discovers an AMBIT over serial, keeps its firmware safely current,
calibrates the PAR sensor and actinic LED against bench references, and records
an OpenJII calibration payload. Firmware binaries are not stored in this repo;
they are fetched from immutable public releases of
[`Jan-IngenHousz-Institute/ambit`](https://github.com/Jan-IngenHousz-Institute/ambit/releases).

## Setup

Use Python 3.10 or newer. From the repository root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install -r requirements.txt
```

The calibration flow expects an AMBIT and, depending on the operation, a WCH
CH343 flashing bridge, MiniPAR reference instruments, and a Kiprim DC source.
`run_Calibratron.py` discovers what is connected and reports missing reference
instruments. Review the tunables near the top of that file before a bench run:
`UPLOAD_GAINS`, `FORCE_FLASH_FIRMWARE`, `ALLOW_FIRMWARE_DOWNGRADE`, and
`RENAME_AMBIT`.

## Firmware source and selection policy

`firmware_fetch.py` uses anonymous, read-only GitHub REST requests by default.
Before accepting a release it proves that:

- the exact source repository is public and active;
- the release is published, non-draft, and immutable;
- every required release asset is fully uploaded and uses the canonical repository/tag
  download URL;
- the manifest is for `esp32c3`, has a safe semantic version and safe filenames,
  and describes the four canonical flash regions;
- every image has a positive size and lowercase SHA-256, with exact agreement
  between the manifest, GitHub REST metadata, downloaded bytes, and cached file;
- the manifest itself matches its REST size and SHA-256 digest.

The current explicitly approved prerelease is immutable `v1.1.3-rc1`. The
default policy is **newest eligible stable, otherwise that one approved
prerelease**. An immutable stable release with numeric version `1.1.3` or newer
therefore becomes the default automatically (for example, `v1.1.4`); a new
prerelease is never selected until its tag is explicitly approved in code
review.

AMBIT reports only a numeric version on-device. Release `1.1.3-rc1` is therefore
intentionally treated as device-visible `1.1.3`, preventing repeat flashing of
an already-current device.

## Cache and offline operation

Verified releases are stored under the ignored `firmware_cache/` directory:

```text
firmware_cache/<version>/
├── manifest.json
├── release-metadata.json
├── bootloader.bin
├── partitions.bin
├── boot_app0.bin
└── ambit-fw-v<version>.bin
```

`release-metadata.json` retains the repository/release REST proof. A cache entry
without that proof, a required digest or size, or any matching asset is rejected.
Existing files are re-hashed on every cache eligibility check.

If GitHub is unreachable or its newest metadata does not satisfy the contract,
the newest complete proven cache is returned with a warning. Cache integrity is
independent of the current selection policy, so a previously verified immutable
release remains usable offline. That does not authorize a flash: the runner
compares numeric versions and upgrades only when the target is newer. An equal
target is skipped, and an older offline target never downgrades a newer device
automatically.

To pre-populate a cache without connecting or modifying hardware:

```bash
python firmware_fetch.py firmware_cache
```

`GITHUB_TOKEN` or `GH_TOKEN` is optional and only raises the API rate limit; the
repository must still prove publicly visible.

## Flashing and downgrade safety

Normal `run_Calibratron.py` operation is strictly upgrade-only:

- older device → flash the newer verified target;
- equivalent numeric version → skip;
- newer device → skip;
- unknown/unparseable device version → skip normally; force enables recovery.

`FORCE_FLASH_FIRMWARE = True` permits a same-version reflash and recovery of a
blank/unresponsive device whose version cannot be read, but does **not** permit
a known downgrade. A target older than a known device requires the separate
`ALLOW_FIRMWARE_DOWNGRADE = True` switch as well. Downgrade is a deliberate
service operation: confirm the selected target printed by the runner and
preserve device/calibration data first.

Do not disconnect USB power during an esptool write. The runner never executes a
flash merely by importing a module or pre-populating the cache; flashing occurs
only through the explicit runner path with a uniquely detected CH343 bridge.

## Calibration safety

- Confirm the reference MiniPAR placement, DC-source current limits, and AMBIT
  optical alignment before starting.
- The ADPD baseline step writes nothing until the operator installs the dark
  fixture and types `DARK`. It measures first, rejects an unsafe `s_630`
  baseline, then saves all six channels atomically and verifies them after a
  reboot.
- Set `UPLOAD_GAINS = False` to inspect fits without writing calibration gains.
- PAR and LED gains use a through-origin fit. Writes are blocked unless all
  samples are finite, non-negative, sufficiently ranged and monotonic, with
  R² at least 0.99, normalized RMSE at most 5%, maximum residual at most 10%
  of full scale, and free-fit intercept at most 5% of full scale.
- Every written gain is read back after reboot. A mismatch triggers restoration
  and verification of the previous value.
- Keep `RENAME_AMBIT = False` unless a device rename is intended.
- Calibration payloads are written to ignored `calibrations/` before optional
  MQTT publication. They include the revalidated immutable release, manifest,
  asset IDs, URLs, sizes, and SHA-256 digests selected for that session. Check
  certificate paths and topic configuration separately.

## Tests

The firmware tests are stdlib-only and do not touch hardware or releases:

```bash
python -m unittest discover -s tests -v
python -m py_compile firmware_fetch.py run_Calibratron.py helpers.py
```

Fixtures under `tests/fixtures/` capture the anonymous production REST/manifest
contract for `v1.1.3-rc1`. Tests cover selection policy, future stable releases,
public/immutable state, canonical URLs, missing and mismatched size/digest data,
unsafe paths, wrong chip, cache tampering, offline fallback, prerelease identity,
upgrade-only behavior, explicit downgrade control, the calibration fit gates,
and the merged OpenJII spectrometer/leaf behavior. GitHub Actions runs the same
suite on pushes and PRs.

## Release provenance: `v1.1.3-rc1`

Anonymous GitHub REST inspection on 2026-08-06 showed public repository
`Jan-IngenHousz-Institute/ambit`, release id `365792568`, published
`2026-08-05T20:14:20Z`, `draft=false`, `prerelease=true`, and `immutable=true`.

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| `manifest.json` | 849 | `bc6ecd522c768e97d770cb2c552761de33f7adddfe78dd42274377452849e7df` |
| `bootloader.bin` | 13,248 | `30d47ab1f344cfa69f6b2f718ffa72fc7baea6db047eaabefea162db29e6821c` |
| `partitions.bin` | 3,072 | `148b959cbff1c38aa8e1d5c0ba9d612c54997b945e56a63f41223eef650653a1` |
| `boot_app0.bin` | 8,192 | `f94c5d786a7a8fab06ac5d10e33bf37711a6697636dc037559ea19cc410a17f0` |
| `ambit-fw-v1.1.3-rc1.bin` | 426,912 | `811a5e2a955fba1eb990d05d25b5bf42ac9144eedd5f364dcb4f3414994626e4` |

## Troubleshooting

- **HTTP 403 / rate limit:** wait for the anonymous limit to reset or export a
  read-only `GITHUB_TOKEN`. The public-repository check is still mandatory.
- **No usable firmware in cache:** connect once and run the pre-population
  command, or copy the entire proven `<version>/` directory including
  `release-metadata.json` from a trusted bench.
- **Cache rejected:** do not edit cached metadata or binaries. Remove only the
  affected version directory and fetch it again when online.
- **Firmware target skipped as older:** this is downgrade protection, not a
  fetch failure. Use the newer device firmware unless an explicit service
  downgrade has been reviewed.
- **No or multiple flashing ports:** connect exactly one AMBIT CH343 bridge and
  retry; the tool refuses to guess.
- **Firmware version unknown:** normal flow fails closed. Inspect the serial boot
  log and connection; use `FORCE_FLASH_FIRMWARE` only for deliberate recovery.

## Desktop GUI

The desktop interface is extracted from PR #4. It uses this branch's existing
PAR origin fit (`light_slope`) and actinic LED calibration, including quality
gates and write/readback verification. The cmd-35 spectral/tier-3 calibration
changes remain in PR #4. Dark-baseline calibration is still available through
the CLI; the GUI handles PAR and LED sweeps.

Download the ZIP for your platform from this repository's GitHub Releases:

- `calibratron-windows-x64.zip`: extract the whole folder and run `calibratron.exe`.
- `calibratron-linux-x64.zip`: extract and run `calibratron/calibratron` (Ubuntu 22.04 or newer).
- `calibratron-macos-arm64.zip`: extract and open `calibratron.app` (Apple Silicon).

Keep the bundle's supporting files beside the executable. Python, notebooks,
and esptool do not need to be installed separately. Serial drivers and the bench
hardware are still required. These initial bundles are unsigned; Windows and
macOS may display an unknown-publisher warning. Intel macOS is not yet packaged.

For a source checkout, install `requirements-gui.txt`, then run
`python calibratron_gui.py`. Linux also needs the distribution's `python3-tk`.
Use **Rescan bench**, select the steps, and press **Start calibration**.
Firmware force-reflash and OpenJII publishing default to off. OpenJII API-key
sign-in is optional; calibration records are saved locally before upload.
A local firmware folder must include the verified immutable release metadata,
manifest, and matching images, just like the CLI's cache.

Packaged applications save `calibrations/` and `firmware_cache/` beneath:

| Platform | Data directory |
| --- | --- |
| Windows | `%LOCALAPPDATA%/Calibratron` |
| macOS | `~/Library/Application Support/Calibratron` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/Calibratron` |

Set `CALIBRATRON_DATA_DIR` to override packaged storage. Source checkouts retain
the existing repository-local storage. Closing the GUI preserves the verified
firmware cache for offline use. API-key settings use the separate per-user
configuration directory managed by `openjii_auth.py`.

## GUI release pipeline

`.github/workflows/gui-release.yml` tests and builds Windows x64, Linux x64,
and macOS ARM64 bundles on pull requests, pushes to `main`, and manual runs.
Every platform runs the packaged executable's `--smoke-test`, which constructs
the GUI and exercises bundled esptool without connecting to hardware or signing
in. PR and manual runs provide downloadable Actions artifacts.

After all three builds pass on `main`, the same run's ZIPs and `SHA256SUMS` are
uploaded to a draft release and then published. Tags start at
`calibratron-v0.1.0` and automatically increment the patch number. Rerunning an
already published commit does not create another release. Publication uses only
the built-in `GITHUB_TOKEN`; no extra release secret or hand-created tag is
needed. Unlike Ambyte's promotion workflow, this workflow builds the merged
`main` commit, so merge commits, squash merges, and direct pushes work alike.

Local verification:

```bash
python -m pip install -r requirements-build.txt
python -m pytest tests -q
pyinstaller --noconfirm --clean --onedir --windowed --name calibratron \
  --paths . --collect-all esptool packaging/launcher.py
# Linux requires a display, or: xvfb-run -a ...
dist/calibratron/calibratron --smoke-test
```

The original stdlib-only firmware tests can still run without GUI dependencies.
Automated packaging checks do not replace a physical calibration bench test.
