"""Fetch the compiled Ambit firmware from the ambit-iot GitHub releases.

The bench used to carry a vendored copy of the firmware images in
``firmware_ambit/``, which meant every firmware bump needed a commit here and
benches silently ran whatever was checked in. Instead this module pulls the
*latest published release* of the firmware repo into a local cache:

    firmware_cache/<version>/manifest.json
    firmware_cache/<version>/bootloader.bin
    firmware_cache/<version>/partitions.bin
    firmware_cache/<version>/boot_app0.bin
    firmware_cache/<version>/ambit-fw-v<version>.bin

``manifest.json`` is the contract with the firmware repo: it names the chip and
lists, for every image, the file name, the flash ``offset`` and a ``sha256``.
The flasher in ``helpers.py`` reads the offsets straight out of it, so a layout
change on the firmware side needs no change here.

Design notes (all of these are field-bench requirements):
  - stdlib only. The benches are plain Windows Python installs and the deps in
    requirements.txt are auto-installed at runtime; a firmware download must not
    depend on that having worked.
  - the manifest is written *last*, so "manifest present and every file it lists
    verifies" is the definition of a complete cache entry. An interrupted
    download can therefore never be mistaken for a usable firmware.
  - when GitHub is unreachable (no uplink on the bench, API rate limit, repo
    without releases yet) we fall back to the newest complete cache entry and
    say so loudly, rather than blocking calibration work.
  - a sha256 mismatch is never tolerated: the file is deleted and the fetch
    fails. Flashing a corrupt image bricks the device until someone reflashes
    over the CH343 bridge by hand.
"""

import hashlib
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


class _UnicodeSafeHandler(logging.StreamHandler):
    """StreamHandler that falls back to ASCII+backslashreplace when the
    underlying stream's encoding (e.g. Windows cp1252) can't render a char.
    Same guard as in helpers.py; kept local so this module stays stdlib-only.
    """
    def emit(self, record):
        try:
            msg = self.format(record) + self.terminator
            try:
                self.stream.write(msg)
            except UnicodeEncodeError:
                self.stream.write(msg.encode("ascii", "backslashreplace").decode("ascii"))
            self.flush()
        except Exception:
            self.handleError(record)


logger = logging.getLogger(__name__)
if not logger.handlers:
    _h = _UnicodeSafeHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


# ============================================================================
# Configuration
# ============================================================================

# Firmware source of truth. Its release pipeline publishes manifest.json plus
# one .bin per flash region for every tagged release.
FIRMWARE_REPO = "Jan-IngenHousz-Institute/ambit-iot"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{FIRMWARE_REPO}/releases/latest"

MANIFEST_NAME = "manifest.json"

# GitHub rejects API requests without a User-Agent.
USER_AGENT = "ambit-Calibratron-firmware-fetch"

# Anonymous API calls are rate-limited to 60/hour per IP, which a bench room
# behind one NAT can burn through. Export GITHUB_TOKEN to lift that (and to
# reach the repo at all while it is private).
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")

HTTP_TIMEOUT = 60          # seconds, per request
_HASH_CHUNK = 1024 * 1024  # 1 MiB, read size for sha256


# ============================================================================
# Small helpers
# ============================================================================

def _auth_token():
    """Return the GitHub token from the environment, or None."""
    for name in TOKEN_ENV_VARS:
        token = (os.environ.get(name) or "").strip()
        if token:
            return token
    return None


def _request(url, accept):
    """Build a urllib Request with the headers GitHub expects."""
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    token = _auth_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def _http_get(url, accept="application/octet-stream"):
    """GET ``url`` and return the raw body.

    :raises urllib.error.URLError: on any transport / HTTP error (HTTPError is
        a subclass, so a 404 "no releases yet" lands here too)
    """
    with urllib.request.urlopen(_request(url, accept), timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


def sha256_file(path):
    """Return the hex sha256 of ``path``, streamed so a 2 MB image is cheap."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_key(version):
    """Sort key for firmware version strings ("0.1.0", "v1.2.3-rc1", ...).

    Numeric components compare numerically (so 0.10.0 > 0.9.0, which a plain
    string sort gets wrong) and a pre-release sorts *below* the release it
    leads up to, mirroring semver.
    """
    core, _, pre = str(version).strip().lstrip("vV").partition("-")
    nums = []
    for part in core.split("."):
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    # A release (no pre-release suffix) ranks above any pre-release of it.
    return (tuple(nums), 0 if pre else 1, pre)


def read_manifest(version_dir):
    """Load ``manifest.json`` from ``version_dir``.

    :return: the parsed manifest dict, or None if it is missing / unreadable /
        not valid JSON (all of which mean "this cache entry is incomplete")
    """
    path = Path(version_dir) / MANIFEST_NAME
    try:
        with open(path, encoding="utf-8") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return manifest if isinstance(manifest, dict) else None


def manifest_entries(manifest):
    """Return the manifest's ``flash`` array as a list of (offset, file, sha256).

    :raises ValueError: if the array is missing, empty, or an entry lacks a
        file name or an offset (an unusable manifest, not a partial download)
    """
    entries = manifest.get("flash") if isinstance(manifest, dict) else None
    if not entries:
        raise ValueError("manifest has no 'flash' entries")

    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"malformed 'flash' entry: {entry!r}")
        name = entry.get("file")
        offset = entry.get("offset")
        if not name or offset is None:
            raise ValueError(f"'flash' entry missing file/offset: {entry!r}")
        out.append((str(offset), str(name), (entry.get("sha256") or "").strip().lower()))
    return out


def is_complete(version_dir):
    """True when ``version_dir`` holds a manifest plus every file it lists,
    each matching its sha256.

    This is the only test used to decide whether a cache entry may be flashed,
    so it deliberately re-hashes: a truncated download that happens to have the
    right file name must not pass.
    """
    version_dir = Path(version_dir)
    manifest = read_manifest(version_dir)
    if manifest is None:
        logger.debug("cache %s incomplete: no readable %s", version_dir, MANIFEST_NAME)
        return False
    try:
        entries = manifest_entries(manifest)
    except ValueError as exc:
        logger.debug("cache %s incomplete: %s", version_dir, exc)
        return False

    for _offset, name, want_sha in entries:
        path = version_dir / name
        if not path.is_file():
            logger.debug("cache %s incomplete: missing %s", version_dir, name)
            return False
        if want_sha and sha256_file(path) != want_sha:
            logger.debug("cache %s incomplete: sha256 mismatch on %s", version_dir, name)
            return False
    return True


def cached_versions(cache_root):
    """List the complete cached firmware versions, oldest first.

    :param cache_root: the ``firmware_cache`` folder (may not exist)
    :return: list of (version, Path) for every complete cache entry
    """
    cache_root = Path(cache_root)
    if not cache_root.is_dir():
        return []
    found = []
    for child in sorted(cache_root.iterdir()):
        if not child.is_dir():
            continue
        if is_complete(child):
            found.append((child.name, child))
        else:
            logger.debug("ignoring incomplete firmware cache entry: %s", child)
    found.sort(key=lambda item: version_key(item[0]))
    return found


def newest_cached(cache_root):
    """Return (version, Path) of the newest complete cache entry, or None."""
    versions = cached_versions(cache_root)
    return versions[-1] if versions else None


# ============================================================================
# Download
# ============================================================================

def _download_asset(url, dest, want_sha):
    """Download ``url`` to ``dest``, verifying ``want_sha`` before committing.

    Already-present files with a matching hash are left alone, so re-running a
    bench session over a metered uplink costs one API call and nothing else.

    :raises RuntimeError: on a sha256 mismatch (the bad file is removed first)
    :raises urllib.error.URLError: if the download itself fails
    """
    dest = Path(dest)
    if dest.is_file() and want_sha and sha256_file(dest) == want_sha:
        logger.info("cached %s (sha256 ok)", dest.name)
        return

    logger.info("downloading %s ...", dest.name)
    body = _http_get(url)

    # Write to a sidecar first: a half-written image must never sit under the
    # name the flasher will hand to esptool.
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(body)

    if want_sha:
        got = sha256_file(part)
        if got != want_sha:
            part.unlink(missing_ok=True)
            raise RuntimeError(
                f"sha256 mismatch for {dest.name}: manifest says {want_sha}, "
                f"downloaded file hashes to {got} - refusing to flash it"
            )
    else:
        logger.warning("manifest has no sha256 for %s - cannot verify it", dest.name)

    dest.unlink(missing_ok=True)
    part.replace(dest)


def _release_assets(release):
    """Map asset name -> download URL for a GitHub release payload."""
    assets = {}
    for asset in release.get("assets") or []:
        name = asset.get("name")
        url = asset.get("browser_download_url")
        if name and url:
            assets[str(name)] = str(url)
    return assets


def _download_release(cache_root):
    """Fetch the latest release into ``cache_root/<version>/``.

    :return: (version, Path) of the freshly populated cache entry
    :raises urllib.error.URLError: if GitHub cannot be reached (404 included)
    :raises RuntimeError: if the release is unusable (no manifest asset, a
        listed file is not published, a sha256 does not match)
    """
    cache_root = Path(cache_root)
    logger.info("querying %s for the latest firmware release", LATEST_RELEASE_URL)
    release = json.loads(_http_get(LATEST_RELEASE_URL, accept="application/vnd.github+json"))
    tag = str(release.get("tag_name") or "").strip()

    assets = _release_assets(release)
    if MANIFEST_NAME not in assets:
        raise RuntimeError(
            f"release {tag or '<untagged>'} of {FIRMWARE_REPO} publishes no "
            f"{MANIFEST_NAME} asset (found: {', '.join(sorted(assets)) or 'nothing'})"
        )

    # The manifest names the version, so it decides the cache folder. Fall back
    # to the tag when the field is absent.
    manifest_bytes = _http_get(assets[MANIFEST_NAME], accept="application/octet-stream")
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{MANIFEST_NAME} of release {tag!r} is not valid JSON: {exc}") from exc
    version = str(manifest.get("version") or tag.lstrip("vV")).strip()
    if not version:
        raise RuntimeError(f"cannot determine a version for release {tag!r} of {FIRMWARE_REPO}")

    try:
        entries = manifest_entries(manifest)
    except ValueError as exc:
        raise RuntimeError(f"{MANIFEST_NAME} of release {tag!r} is unusable: {exc}") from exc

    version_dir = cache_root / version
    version_dir.mkdir(parents=True, exist_ok=True)

    # Drop any existing manifest up front: while we are downloading, this cache
    # entry is *not* complete, and a stale manifest would claim otherwise if the
    # run is interrupted.
    (version_dir / MANIFEST_NAME).unlink(missing_ok=True)

    logger.info("firmware %s (chip %s), %d image(s) -> %s",
                version, manifest.get("chip") or "?", len(entries), version_dir)
    for _offset, name, want_sha in entries:
        if name not in assets:
            raise RuntimeError(
                f"{MANIFEST_NAME} of release {tag!r} lists {name!r} but the "
                f"release does not publish it"
            )
        _download_asset(assets[name], version_dir / name, want_sha)

    # Written last: this is what marks the cache entry as complete.
    (version_dir / MANIFEST_NAME).write_bytes(manifest_bytes)
    logger.info("firmware %s ready in %s", version, version_dir)
    return version, version_dir


def fetch_latest(cache_root):
    """Make the latest published Ambit firmware available on disk.

    Queries the ambit-iot releases API and downloads whatever ``manifest.json``
    lists into ``cache_root/<version>/``. If GitHub cannot be reached (offline
    bench, rate limit, no release published yet) the newest complete cache entry
    is used instead, with a warning.

    :param cache_root: the ``firmware_cache`` folder (created on demand)
    :return: (version, Path) - the version string and the folder holding the
        manifest plus the images, ready to hand to helpers.flash_ambit_firmware
    :raises RuntimeError: if the release cannot be used (bad manifest, sha256
        mismatch, missing asset) or if GitHub is unreachable and the cache is
        empty
    """
    cache_root = Path(cache_root)
    try:
        return _download_release(cache_root)
    except urllib.error.URLError as exc:
        # HTTPError is a URLError; a 404 here is the "repo has no releases yet"
        # case, which is expected until the firmware release pipeline runs once.
        reason = _describe_url_error(exc)
        fallback = newest_cached(cache_root)
        if fallback is None:
            raise RuntimeError(
                f"cannot fetch the Ambit firmware from {FIRMWARE_REPO}: {reason}. "
                f"No usable firmware in the local cache ({cache_root}) either. "
                f"If {FIRMWARE_REPO} has not published a release yet, wait for its "
                f"release pipeline to run; if the bench is offline, copy a "
                f"firmware_cache/<version>/ folder (manifest.json + images) from a "
                f"machine that has one. Set GITHUB_TOKEN if this is a rate limit or "
                f"a private-repo error."
            ) from exc
        version, version_dir = fallback
        logger.warning(
            "WARNING: could not reach %s (%s) - falling back to the newest cached "
            "firmware %s in %s. It may not be the latest release.",
            FIRMWARE_REPO, reason, version, version_dir,
        )
        return version, version_dir


def _describe_url_error(exc):
    """Human-readable one-liner for a urllib error, 404s spelled out."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return (f"HTTP 404 - {FIRMWARE_REPO} has no published release "
                    f"(or the repo/token is not visible to us)")
        if exc.code in (401, 403):
            return (f"HTTP {exc.code} - not authorised (private repo or API rate "
                    f"limit); set GITHUB_TOKEN")
        return f"HTTP {exc.code} {exc.reason}"
    return f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"


if __name__ == "__main__":
    # Handy on a bench: `python firmware_fetch.py [cache_root]` pre-populates
    # the cache before the calibration run, and prints where it landed.
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "firmware_cache"
    try:
        ver, path = fetch_latest(root)
    except (urllib.error.URLError, RuntimeError) as err:
        logger.error("%s", err)
        raise SystemExit(1)
    logger.info("firmware %s available at %s", ver, path)
