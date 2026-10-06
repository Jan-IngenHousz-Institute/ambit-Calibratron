"""Fetch verified Ambit firmware from the public AMBIT GitHub releases.

The bench used to carry a vendored copy of the firmware images in
``firmware_ambit/``, which meant every firmware bump needed a commit here and
benches silently ran whatever was checked in. Instead this module selects an
approved, immutable release from the public firmware repo into a local cache:

    firmware_cache/<version>/manifest.json
    firmware_cache/<version>/release-metadata.json
    firmware_cache/<version>/bootloader.bin
    firmware_cache/<version>/partitions.bin
    firmware_cache/<version>/boot_app0.bin
    firmware_cache/<version>/ambit-fw-v<version>.bin

``manifest.json`` and GitHub's REST asset metadata form one contract. File
names, sizes, and SHA-256 digests must agree before an image enters the cache.
``release-metadata.json`` retains the public/published/non-draft/immutable REST
proof so offline cache checks remain fail-closed.

Design notes (all of these are field-bench requirements):
  - stdlib only. The benches are plain Windows Python installs and the deps in
    requirements.txt are auto-installed at runtime; a firmware download must not
    depend on that having worked.
  - the manifest is written *last*, so "manifest present and every file it lists
    verifies" is the definition of a complete cache entry. An interrupted
    download can therefore never be mistaken for a usable firmware.
  - the explicitly approved prerelease is ``v1.1.3-rc1``. A newer immutable
    stable release becomes the default automatically; no other prerelease is
    eligible without a code-reviewed policy update.
  - when GitHub is unreachable or its newest release is ineligible, we fall
    back to the newest complete cache entry.
    The caller still compares its numeric device-visible version and never
    flashes an equal or older target in the normal flow.
  - any missing/malformed digest, size disagreement, unsafe path, wrong chip,
    mutable release, or corrupt byte is rejected. A corrupt image can otherwise
    brick the device until it is reflashed over the CH343 bridge by hand.
"""

import hashlib
import http.client
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
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
FIRMWARE_REPO = "Jan-IngenHousz-Institute/ambit"
REPOSITORY_URL = f"https://api.github.com/repos/{FIRMWARE_REPO}"
RELEASES_URL = f"{REPOSITORY_URL}/releases?per_page=100"

# Release policy: the only prerelease approved for unattended benches is the
# immutable public v1.1.3-rc1 release. Once an immutable stable release with an
# equal or newer numeric core exists, the newest stable becomes the default.
# Approving another prerelease requires changing this constant in review.
APPROVED_PRERELEASE_TAG = "v1.1.3-rc1"
EXPECTED_CHIP = "esp32c3"
EXPECTED_MANIFEST_NAME = "ambit-iot"

MANIFEST_NAME = "manifest.json"
RELEASE_METADATA_NAME = "release-metadata.json"

# GitHub rejects API requests without a User-Agent.
USER_AGENT = "ambit-Calibratron-firmware-fetch"

# Anonymous API calls are rate-limited to 60/hour per IP, which a bench room
# behind one NAT can burn through. Export GITHUB_TOKEN to lift that limit. A
# token never relaxes the mandatory public-repository check.
TOKEN_ENV_VARS = ("GITHUB_TOKEN", "GH_TOKEN")

HTTP_TIMEOUT = 60          # seconds, per request
_HASH_CHUNK = 1024 * 1024  # 1 MiB, read size for sha256
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(
    r"^v?(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z][0-9A-Za-z.-]*))?$"
)


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
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _auth_token()
    # Release assets are public browser URLs which redirect to GitHub's CDN.
    # Never put a token on them: urllib preserves Authorization on redirects.
    # Authentication is useful only on the GitHub API host for rate limits.
    if token and urllib.parse.urlsplit(url).hostname == "api.github.com":
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


def sha256_bytes(body):
    """Return the lowercase SHA-256 of a byte string."""
    return hashlib.sha256(body).hexdigest()


def _version_match(version):
    match = _VERSION_RE.fullmatch(str(version).strip())
    if match is None:
        raise ValueError(f"invalid firmware version {version!r}")
    return match


def numeric_version(version):
    """Return the device-visible numeric identity of a release/device version.

    AMBIT firmware reports only the numeric core on-device, so release
    ``1.1.3-rc1`` is deliberately equivalent to device string ``1.1.3``.
    """
    match = _version_match(version)
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def device_visible_version(version):
    """Return a canonical ``major.minor.patch`` string visible on the device."""
    return ".".join(str(part) for part in numeric_version(version))


def compare_device_versions(current, target):
    """Compare two versions by device-visible numeric identity.

    :return: -1 when current is older, 0 when equivalent, 1 when current is
        newer. Invalid/unknown strings raise ``ValueError`` so callers cannot
        accidentally turn ambiguity into permission to flash.
    """
    current_key = numeric_version(current)
    target_key = numeric_version(target)
    return (current_key > target_key) - (current_key < target_key)


def flash_decision(current, target, *, force=False, allow_downgrade=False):
    """Return the safe flashing action for a current and target version.

    Actions are ``upgrade``, ``reflash``, ``recovery``, ``downgrade``,
    ``equivalent``, ``newer``, or ``unknown``. Normal flow authorizes only
    ``upgrade``. Force permits equivalent reflash or recovery when the current
    identity is unknown. A known newer version still requires the separate
    explicit downgrade override.
    """
    try:
        relation = compare_device_versions(current, target)
    except (TypeError, ValueError):
        return "recovery" if force else "unknown"
    if relation < 0:
        return "upgrade"
    if relation == 0:
        return "reflash" if force else "equivalent"
    return "downgrade" if allow_downgrade else "newer"


def version_key(version):
    """Sort key for firmware version strings ("0.1.0", "v1.2.3-rc1", ...).

    Numeric components compare numerically (so 0.10.0 > 0.9.0, which a plain
    string sort gets wrong) and a pre-release sorts *below* the release it
    leads up to, mirroring semver.
    """
    match = _version_match(version)
    nums = tuple(int(match.group(name)) for name in ("major", "minor", "patch"))
    pre = match.group("pre") or ""
    # A release (no pre-release suffix) ranks above any pre-release of it.
    return (nums, 0 if pre else 1, pre)


def _safe_component(value, label):
    """Validate a single relative filename/path component."""
    value = str(value or "")
    if (not value or value in (".", "..") or Path(value).name != value
            or "/" in value or "\\" in value or "\x00" in value):
        raise ValueError(f"unsafe {label}: {value!r}")
    return value


def _valid_sha256(value, label="sha256"):
    value = str(value or "")
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be 64 lowercase hexadecimal characters")
    return value


def _positive_size(value, label="size"):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return value


def read_manifest(version_dir):
    """Load the cached manifest dict, or ``None`` when unreadable."""
    manifest = _read_json(Path(version_dir) / MANIFEST_NAME)
    return manifest if isinstance(manifest, dict) else None


def read_release_metadata(version_dir):
    """Load retained GitHub repository/release proof from a cache entry."""
    metadata = _read_json(Path(version_dir) / RELEASE_METADATA_NAME)
    return metadata if isinstance(metadata, dict) else None


def validate_public_repository(repository):
    """Require an anonymous-public, active repository REST payload."""
    if not isinstance(repository, dict):
        raise ValueError("repository metadata is not an object")
    if repository.get("full_name") != FIRMWARE_REPO:
        raise ValueError(f"repository identity is not {FIRMWARE_REPO}")
    if repository.get("private") is not False or repository.get("visibility") != "public":
        raise ValueError(f"repository {FIRMWARE_REPO} is not publicly visible")
    if repository.get("archived") is True or repository.get("disabled") is True:
        raise ValueError(f"repository {FIRMWARE_REPO} is archived or disabled")
    return repository


def _canonical_asset_url(tag, name):
    quoted_tag = urllib.parse.quote(tag, safe="")
    quoted_name = urllib.parse.quote(name, safe="")
    return f"https://github.com/{FIRMWARE_REPO}/releases/download/{quoted_tag}/{quoted_name}"


def validate_release(release):
    """Validate immutable published release metadata and canonical assets.

    :return: ``(tag, assets_by_name)`` where each asset retains its REST size,
        digest, and canonical browser download URL. Unrelated release assets
        such as SBOMs or debug maps are deliberately ignored.
    """
    if not isinstance(release, dict):
        raise ValueError("release metadata is not an object")
    tag = _safe_component(release.get("tag_name"), "release tag")
    match = _version_match(tag)
    if (isinstance(release.get("id"), bool)
            or not isinstance(release.get("id"), int) or release["id"] <= 0):
        raise ValueError(f"release {tag} has no numeric REST id")
    if release.get("draft") is not False:
        raise ValueError(f"release {tag} is a draft")
    if not isinstance(release.get("published_at"), str) or not release["published_at"].strip():
        raise ValueError(f"release {tag} is not published")
    if release.get("immutable") is not True:
        raise ValueError(f"release {tag} is mutable")
    if not isinstance(release.get("prerelease"), bool):
        raise ValueError(f"release {tag} has no prerelease state")
    if release["prerelease"] != (match.group("pre") is not None):
        raise ValueError(f"release {tag} prerelease state disagrees with its tag")

    raw_assets = {}
    release_assets = release.get("assets")
    if not isinstance(release_assets, list) or not release_assets:
        raise ValueError(f"release {tag} has no assets")
    for asset in release_assets:
        if not isinstance(asset, dict):
            raise ValueError(f"release {tag} has malformed asset metadata")
        name = _safe_component(asset.get("name"), "asset name")
        if name in raw_assets:
            raise ValueError(f"release {tag} contains duplicate asset {name!r}")
        raw_assets[name] = asset

    version = tag[1:] if tag.startswith("v") else tag
    required_names = {
        MANIFEST_NAME,
        "bootloader.bin",
        "partitions.bin",
        "boot_app0.bin",
        f"ambit-fw-v{version}.bin",
    }
    assets = {}
    for name in required_names:
        asset = raw_assets.get(name)
        if asset is None:
            raise ValueError(f"release {tag} does not publish required asset {name!r}")
        if (isinstance(asset.get("id"), bool)
                or not isinstance(asset.get("id"), int) or asset["id"] <= 0):
            raise ValueError(f"release asset {name!r} has no numeric REST id")
        if asset.get("state") != "uploaded":
            raise ValueError(f"release asset {name!r} is not fully uploaded")
        size = _positive_size(asset.get("size"), f"REST size for {name}")
        digest = str(asset.get("digest") or "")
        if not digest.startswith("sha256:"):
            raise ValueError(f"REST digest for {name} is not SHA-256")
        sha256 = _valid_sha256(digest[len("sha256:"):], f"REST digest for {name}")
        url = str(asset.get("browser_download_url") or "")
        canonical = _canonical_asset_url(tag, name)
        if url != canonical:
            raise ValueError(f"release asset {name!r} has non-canonical download URL")
        assets[name] = {
            "id": asset["id"],
            "name": name,
            "size": size,
            "sha256": sha256,
            "url": url,
        }
    return tag, assets


def select_release(releases):
    """Apply the stable-or-approved-prerelease selection policy.

    New immutable stable releases at or above the approved prerelease's numeric
    core win. Until one exists, exactly ``APPROVED_PRERELEASE_TAG`` is selected;
    arbitrary prereleases are never chosen.
    """
    if not isinstance(releases, list):
        raise ValueError("GitHub releases response is not an array")

    approved = []
    stable = []
    approved_core = numeric_version(APPROVED_PRERELEASE_TAG)
    for release in releases:
        if not isinstance(release, dict):
            continue
        tag = str(release.get("tag_name") or "").strip()
        is_approved = tag == APPROVED_PRERELEASE_TAG
        try:
            is_stable = (
                release.get("prerelease") is False
                and numeric_version(tag) >= approved_core
            )
        except ValueError:
            is_stable = False
        if not is_approved and not is_stable:
            continue
        try:
            validate_release(release)
        except ValueError as exc:
            logger.warning("ignoring ineligible release %s: %s", tag or "<untagged>", exc)
            continue
        if is_approved:
            approved.append(release)
        if is_stable:
            stable.append(release)

    if stable:
        selected = max(stable, key=lambda item: version_key(item.get("tag_name")))
    elif len(approved) == 1:
        selected = approved[0]
        if selected.get("prerelease") is not True:
            raise ValueError(f"approved tag {APPROVED_PRERELEASE_TAG} is not marked prerelease")
    elif len(approved) > 1:
        raise ValueError(f"GitHub returned duplicate releases for {APPROVED_PRERELEASE_TAG}")
    else:
        raise ValueError(
            f"no eligible stable release and approved prerelease "
            f"{APPROVED_PRERELEASE_TAG} is absent"
        )

    return selected


def manifest_entries(manifest, *, tag=None, assets=None):
    """Validate and return ``(offset, file, size, sha256)`` flash entries.

    The four canonical ESP32-C3 flash regions are required. When REST assets
    are supplied, names, sizes, and digests must agree exactly.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest is not an object")
    if manifest.get("name") != EXPECTED_MANIFEST_NAME:
        raise ValueError(f"manifest name must be {EXPECTED_MANIFEST_NAME!r}")
    if manifest.get("chip") != EXPECTED_CHIP:
        raise ValueError(f"manifest chip must be {EXPECTED_CHIP!r}")

    version = _safe_component(manifest.get("version"), "manifest version")
    _version_match(version)
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"manifest version {version!r} does not match release tag {tag!r}")

    expected_offsets = {
        "bootloader.bin": "0x0",
        "partitions.bin": "0x8000",
        "boot_app0.bin": "0xe000",
        f"ambit-fw-v{version}.bin": "0x10000",
    }
    entries = manifest.get("flash")
    if not isinstance(entries, list) or not entries:
        raise ValueError("manifest has no 'flash' entries")

    out = []
    seen_names = set()
    seen_offsets = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"malformed 'flash' entry: {entry!r}")
        name = _safe_component(entry.get("file"), "manifest flash filename")
        offset = str(entry.get("offset") or "")
        size = _positive_size(entry.get("size"), f"manifest size for {name}")
        sha256 = _valid_sha256(entry.get("sha256"), f"manifest sha256 for {name}")
        if name in seen_names or offset in seen_offsets:
            raise ValueError("manifest contains a duplicate file or flash offset")
        seen_names.add(name)
        seen_offsets.add(offset)
        if expected_offsets.get(name) != offset:
            raise ValueError(f"manifest has unexpected file/offset {name!r} at {offset!r}")
        if assets is not None:
            asset = assets.get(name)
            if asset is None:
                raise ValueError(f"release does not publish manifest asset {name!r}")
            if asset["size"] != size:
                raise ValueError(f"size disagreement for {name}: manifest {size}, REST {asset['size']}")
            if asset["sha256"] != sha256:
                raise ValueError(f"digest disagreement for {name} between manifest and REST")
        out.append((offset, name, size, sha256))

    if seen_names != set(expected_offsets):
        raise ValueError(
            f"manifest flash assets are not canonical (expected {sorted(expected_offsets)})"
        )
    ota = manifest.get("ota")
    app_name = f"ambit-fw-v{version}.bin"
    if not isinstance(ota, dict) or ota.get("file") != app_name:
        raise ValueError(f"manifest OTA asset must be {app_name!r}")
    if assets is not None:
        missing = (seen_names | {MANIFEST_NAME}) - set(assets)
        if missing:
            raise ValueError(f"release is missing canonical assets: {sorted(missing)}")
    return out


def is_complete(version_dir):
    """Return whether a cache entry passes the retained integrity contract.

    This is deliberately independent of today's selection policy: a previously
    proven immutable cache remains verifiable after the default tag changes.
    """
    version_dir = Path(version_dir)
    manifest_path = version_dir / MANIFEST_NAME
    metadata = read_release_metadata(version_dir)
    manifest = read_manifest(version_dir)
    if metadata is None or manifest is None:
        logger.debug("cache %s incomplete: no readable manifest/provenance", version_dir)
        return False
    try:
        validate_public_repository(metadata.get("repository"))
        release = metadata.get("release")
        tag, assets = validate_release(release)
        manifest_asset = assets.get(MANIFEST_NAME)
        if manifest_asset is None:
            raise ValueError(f"release publishes no {MANIFEST_NAME}")
        if manifest_path.stat().st_size != manifest_asset["size"]:
            raise ValueError("cached manifest size disagrees with REST metadata")
        if sha256_file(manifest_path) != manifest_asset["sha256"]:
            raise ValueError("cached manifest digest disagrees with REST metadata")
        entries = manifest_entries(manifest, tag=tag, assets=assets)
        if version_dir.name != manifest["version"]:
            raise ValueError("cache directory does not match manifest version")
        for _offset, name, want_size, want_sha in entries:
            path = version_dir / name
            if not path.is_file() or path.stat().st_size != want_size:
                raise ValueError(f"missing or wrong-size cached asset {name}")
            if sha256_file(path) != want_sha:
                raise ValueError(f"sha256 mismatch on cached asset {name}")
    except (OSError, TypeError, ValueError) as exc:
        logger.debug("cache %s incomplete: %s", version_dir, exc)
        return False
    return True


def release_provenance(version_dir):
    """Return concise, revalidated release/asset proof for calibration records."""
    version_dir = Path(version_dir)
    if not is_complete(version_dir):
        raise ValueError(f"firmware cache {version_dir} is not complete and verified")
    metadata = read_release_metadata(version_dir)
    manifest = read_manifest(version_dir)
    repository = validate_public_repository(metadata.get("repository"))
    release = metadata.get("release")
    tag, assets = validate_release(release)
    entries = manifest_entries(manifest, tag=tag, assets=assets)
    manifest_asset = assets[MANIFEST_NAME]
    return {
        "repository": repository["full_name"],
        "release_id": release["id"],
        "tag": tag,
        "version": manifest["version"],
        "published_at": release["published_at"],
        "immutable": True,
        "manifest": {
            "asset_id": manifest_asset["id"],
            "size": manifest_asset["size"],
            "sha256": manifest_asset["sha256"],
            "url": manifest_asset["url"],
        },
        "flash": [
            {
                "asset_id": assets[name]["id"],
                "file": name,
                "offset": offset,
                "size": size,
                "sha256": sha256,
                "url": assets[name]["url"],
            }
            for offset, name, size, sha256 in entries
        ],
    }


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

def _atomic_write(dest, body):
    """Write bytes through a sidecar so partial files are never trusted."""
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    part.write_bytes(body)
    part.replace(dest)


def _verify_body(body, asset):
    """Require downloaded bytes to match REST size and digest metadata."""
    if len(body) != asset["size"]:
        raise RuntimeError(
            f"size mismatch for {asset['name']}: REST says {asset['size']}, "
            f"downloaded {len(body)} bytes"
        )
    got = sha256_bytes(body)
    if got != asset["sha256"]:
        raise RuntimeError(
            f"sha256 mismatch for {asset['name']}: REST says {asset['sha256']}, "
            f"downloaded bytes hash to {got} - refusing to flash it"
        )


def _download_asset(asset, dest):
    """Download one validated REST asset, verifying size and SHA-256.

    Already-present files with a matching hash are left alone, so re-running a
    bench session over a metered uplink costs one API call and nothing else.

    :raises RuntimeError: on any size/digest mismatch
    :raises urllib.error.URLError: if the download itself fails
    """
    dest = Path(dest)
    if (dest.is_file() and dest.stat().st_size == asset["size"]
            and sha256_file(dest) == asset["sha256"]):
        logger.info("cached %s (size and sha256 ok)", dest.name)
        return

    logger.info("downloading %s ...", dest.name)
    body = _http_get(asset["url"])
    _verify_body(body, asset)
    _atomic_write(dest, body)


def _json_response(url, label):
    body = _http_get(url, accept="application/vnd.github+json")
    try:
        return json.loads(body)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned invalid JSON: {exc}") from exc


def _download_release(cache_root):
    """Fetch the policy-selected release into ``cache_root/<version>/``.

    :return: (version, Path) of the freshly populated cache entry
    :raises urllib.error.URLError: if GitHub cannot be reached (404 included)
    :raises RuntimeError: if the release is unusable (no manifest asset, a
        listed file is not published, a sha256 does not match)
    """
    cache_root = Path(cache_root)
    logger.info("proving public repository state at %s", REPOSITORY_URL)
    repository = _json_response(REPOSITORY_URL, "repository endpoint")
    try:
        validate_public_repository(repository)
    except ValueError as exc:
        raise RuntimeError(f"firmware repository is unusable: {exc}") from exc

    logger.info("selecting firmware release from %s", RELEASES_URL)
    releases = _json_response(RELEASES_URL, "releases endpoint")
    try:
        release = select_release(releases)
        tag, assets = validate_release(release)
    except ValueError as exc:
        raise RuntimeError(f"no usable firmware release: {exc}") from exc

    manifest_asset = assets.get(MANIFEST_NAME)
    if manifest_asset is None:
        raise RuntimeError(f"release {tag} publishes no {MANIFEST_NAME} asset")
    manifest_bytes = _http_get(manifest_asset["url"], accept="application/octet-stream")
    _verify_body(manifest_bytes, manifest_asset)
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{MANIFEST_NAME} of release {tag!r} is not valid JSON: {exc}") from exc

    try:
        entries = manifest_entries(manifest, tag=tag, assets=assets)
    except ValueError as exc:
        raise RuntimeError(f"{MANIFEST_NAME} of release {tag!r} is unusable: {exc}") from exc

    version = manifest["version"]
    version_dir = cache_root / version
    version_dir.mkdir(parents=True, exist_ok=True)

    logger.info("firmware %s (chip %s), %d image(s) -> %s",
                version, manifest.get("chip") or "?", len(entries), version_dir)
    for _offset, name, _want_size, _want_sha in entries:
        _download_asset(assets[name], version_dir / name)

    # Provenance then manifest are written atomically after all images. The
    # manifest stays the final marker, while a previously valid immutable cache
    # remains usable if a refresh is interrupted before these writes.
    provenance = json.dumps(
        {"repository": repository, "release": release},
        indent=2,
        sort_keys=True,
    ).encode("utf-8")
    _atomic_write(version_dir / RELEASE_METADATA_NAME, provenance)
    _atomic_write(version_dir / MANIFEST_NAME, manifest_bytes)
    if not is_complete(version_dir):
        raise RuntimeError(f"downloaded firmware cache {version_dir} failed final verification")
    logger.info("firmware %s ready in %s", version, version_dir)
    return version, version_dir


def fetch_latest(cache_root):
    """Make the policy-selected immutable Ambit firmware available on disk.

    Proves the source repository public, selects a newer immutable stable release
    when one exists or the explicitly approved prerelease otherwise, and checks
    every byte against both the manifest and REST metadata. If GitHub cannot be
    reached or the newest online metadata is ineligible, the newest fully proven
    local cache entry is used with a warning.

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
    except (OSError, http.client.HTTPException, RuntimeError) as exc:
        # A proven cache is safe when the network is unavailable or the online
        # release is ineligible/malformed. The version comparison at the caller
        # still decides whether that cached target may be flashed.
        reason = _describe_url_error(exc)
        fallback = newest_cached(cache_root)
        if fallback is None:
            raise RuntimeError(
                f"cannot fetch the Ambit firmware from {FIRMWARE_REPO}: {reason}. "
                f"No usable firmware in the local cache ({cache_root}) either. "
                f"If {FIRMWARE_REPO} has not published a release yet, wait for its "
                f"release pipeline to run; if the bench is offline, copy a "
                f"complete firmware_cache/<version>/ folder (manifest.json, "
                f"release-metadata.json, and images) from a "
                f"machine that has one. Set GITHUB_TOKEN if this is a rate limit or "
                f"authorization error."
            ) from exc
        version, version_dir = fallback
        logger.warning(
            "WARNING: could not use online firmware from %s (%s) - falling back "
            "to the newest cached "
            "firmware %s in %s. It may not be the latest release.",
            FIRMWARE_REPO, reason, version, version_dir,
        )
        return version, version_dir


def _describe_url_error(exc):
    """Human-readable one-liner for a urllib error, 404s spelled out."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return (f"HTTP 404 - {FIRMWARE_REPO} or its releases are not visible")
        if exc.code in (401, 403):
            return (f"HTTP {exc.code} - not authorised or API rate limit; "
                    f"set GITHUB_TOKEN")
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
