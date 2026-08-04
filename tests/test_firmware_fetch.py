"""Self-test for firmware_fetch.py: cache verification + offline fallback.

Stdlib only (unittest), no network: the GitHub call is monkeypatched, so this
runs on any bench and in CI. Run it with either of::

    python tests/test_firmware_fetch.py
    python -m unittest discover -s tests

What it pins down (the two things that decide whether a bench flashes a good
image or a corrupt one):
  - a cache entry only counts as complete when the manifest is present *and*
    every file it lists hashes to the sha256 in the manifest;
  - when GitHub is unreachable we fall back to the newest complete cached
    version, and raise a clear error when there is nothing to fall back to.
"""

import hashlib
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import firmware_fetch  # noqa: E402  (needs the sys.path tweak above)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _make_release(version, images=None):
    """Build a (manifest, {name: bytes}) pair shaped like a real release."""
    images = images or {
        "bootloader.bin":  b"bootloader-" + version.encode(),
        "partitions.bin":  b"partitions-" + version.encode(),
        "boot_app0.bin":   b"boot_app0-" + version.encode(),
        f"ambit-fw-v{version}.bin": b"app-" + version.encode(),
    }
    offsets = {"bootloader.bin": "0x0", "partitions.bin": "0x8000",
               "boot_app0.bin": "0xe000"}
    flash = []
    for name, body in images.items():
        flash.append({
            "file": name,
            "offset": offsets.get(name, "0x10000"),
            "size": len(body),
            "sha256": _sha256(body),
        })
    manifest = {
        "name": "ambit-iot",
        "version": version,
        "chip": "esp32c3",
        "flash": flash,
        "ota": {"file": f"ambit-fw-v{version}.bin"},
    }
    return manifest, images


def _write_cache_entry(cache_root, version, *, complete=True, tamper=None,
                       drop=None):
    """Materialise firmware_cache/<version>/ on disk.

    :param complete: when False, the manifest is left out (interrupted download)
    :param tamper: file name whose content should no longer match its sha256
    :param drop: file name to leave out entirely
    """
    manifest, images = _make_release(version)
    version_dir = Path(cache_root) / version
    version_dir.mkdir(parents=True, exist_ok=True)
    for name, body in images.items():
        if name == drop:
            continue
        if name == tamper:
            body = body + b"-corrupted"
        (version_dir / name).write_bytes(body)
    if complete:
        (version_dir / firmware_fetch.MANIFEST_NAME).write_text(
            json.dumps(manifest), encoding="utf-8")
    return version_dir


class _TempCache(unittest.TestCase):
    def setUp(self):
        self.cache_root = Path(tempfile.mkdtemp(prefix="fwcache-"))
        self.addCleanup(shutil.rmtree, self.cache_root, ignore_errors=True)


class TestCacheCompleteness(_TempCache):

    def test_complete_entry_is_accepted(self):
        version_dir = _write_cache_entry(self.cache_root, "0.1.0")
        self.assertTrue(firmware_fetch.is_complete(version_dir))

    def test_missing_manifest_is_incomplete(self):
        version_dir = _write_cache_entry(self.cache_root, "0.1.0", complete=False)
        self.assertFalse(firmware_fetch.is_complete(version_dir))

    def test_tampered_image_is_incomplete(self):
        version_dir = _write_cache_entry(self.cache_root, "0.1.0",
                                         tamper="bootloader.bin")
        self.assertFalse(firmware_fetch.is_complete(version_dir))

    def test_missing_image_is_incomplete(self):
        version_dir = _write_cache_entry(self.cache_root, "0.1.0",
                                         drop="boot_app0.bin")
        self.assertFalse(firmware_fetch.is_complete(version_dir))

    def test_unparseable_manifest_is_incomplete(self):
        version_dir = _write_cache_entry(self.cache_root, "0.1.0")
        (version_dir / firmware_fetch.MANIFEST_NAME).write_text("{ not json",
                                                                encoding="utf-8")
        self.assertFalse(firmware_fetch.is_complete(version_dir))

    def test_newest_cached_ignores_incomplete_and_sorts_numerically(self):
        _write_cache_entry(self.cache_root, "0.9.0")
        _write_cache_entry(self.cache_root, "0.10.0")
        _write_cache_entry(self.cache_root, "1.0.0", tamper="partitions.bin")
        _write_cache_entry(self.cache_root, "2.0.0", complete=False)
        version, path = firmware_fetch.newest_cached(self.cache_root)
        self.assertEqual("0.10.0", version)   # 0.10.0 > 0.9.0, and the newer
        self.assertEqual("0.10.0", path.name)  # entries are both unusable

    def test_version_key_orders_prereleases_below_releases(self):
        ordered = sorted(["1.0.0", "0.1.0", "v1.0.0-rc1", "0.10.0"],
                         key=firmware_fetch.version_key)
        self.assertEqual(["0.1.0", "0.10.0", "v1.0.0-rc1", "1.0.0"], ordered)


class TestFetchLatest(_TempCache):

    def _patch_http(self, handler):
        """Route firmware_fetch's only network call through ``handler(url)``."""
        original = firmware_fetch._http_get
        firmware_fetch._http_get = lambda url, accept=None: handler(url)
        self.addCleanup(setattr, firmware_fetch, "_http_get", original)

    def _release_handler(self, version, *, corrupt=None):
        """Fake GitHub: releases/latest JSON + one URL per asset."""
        manifest, images = _make_release(version)
        assets = {name: f"https://example.invalid/{version}/{name}"
                  for name in list(images) + [firmware_fetch.MANIFEST_NAME]}
        bodies = dict(images)
        bodies[firmware_fetch.MANIFEST_NAME] = json.dumps(manifest).encode()
        if corrupt:
            bodies[corrupt] = bodies[corrupt] + b"-flipped-bit"
        release = {
            "tag_name": f"v{version}",
            "assets": [{"name": n, "browser_download_url": u}
                       for n, u in assets.items()],
        }

        def handler(url):
            if url == firmware_fetch.LATEST_RELEASE_URL:
                return json.dumps(release).encode()
            name = url.rsplit("/", 1)[-1]
            return bodies[name]

        return handler

    def test_downloads_release_into_the_cache(self):
        self._patch_http(self._release_handler("0.1.0"))
        version, path = firmware_fetch.fetch_latest(self.cache_root)
        self.assertEqual("0.1.0", version)
        self.assertEqual(self.cache_root / "0.1.0", path)
        self.assertTrue(firmware_fetch.is_complete(path))
        self.assertTrue((path / "ambit-fw-v0.1.0.bin").is_file())

    def test_sha256_mismatch_fails_and_leaves_no_image_behind(self):
        self._patch_http(self._release_handler("0.1.0", corrupt="bootloader.bin"))
        with self.assertRaises(RuntimeError) as ctx:
            firmware_fetch.fetch_latest(self.cache_root)
        self.assertIn("sha256 mismatch", str(ctx.exception))
        version_dir = self.cache_root / "0.1.0"
        self.assertFalse((version_dir / "bootloader.bin").exists())
        # No manifest -> the half-populated folder can never be flashed.
        self.assertFalse((version_dir / firmware_fetch.MANIFEST_NAME).exists())
        self.assertFalse(firmware_fetch.is_complete(version_dir))

    def test_falls_back_to_newest_complete_cache_when_offline(self):
        _write_cache_entry(self.cache_root, "0.1.0")
        _write_cache_entry(self.cache_root, "0.2.0", tamper="bootloader.bin")

        def offline(url):
            raise urllib.error.URLError("bench has no uplink")

        self._patch_http(offline)
        version, path = firmware_fetch.fetch_latest(self.cache_root)
        self.assertEqual("0.1.0", version)   # 0.2.0 is corrupt -> skipped
        self.assertEqual(self.cache_root / "0.1.0", path)

    def test_no_release_and_no_cache_raises_a_clear_error(self):
        def not_found(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        self._patch_http(not_found)
        with self.assertRaises(RuntimeError) as ctx:
            firmware_fetch.fetch_latest(self.cache_root)
        message = str(ctx.exception)
        self.assertIn("404", message)
        self.assertIn("no published release", message)
        self.assertIn("GITHUB_TOKEN", message)

    def test_release_without_manifest_asset_is_rejected(self):
        def no_manifest(url):
            return json.dumps({"tag_name": "v0.1.0", "assets": []}).encode()

        self._patch_http(no_manifest)
        with self.assertRaises(RuntimeError) as ctx:
            firmware_fetch.fetch_latest(self.cache_root)
        self.assertIn("manifest.json", str(ctx.exception))

    def test_existing_verified_files_are_not_redownloaded(self):
        _write_cache_entry(self.cache_root, "0.1.0")
        downloaded = []
        handler = self._release_handler("0.1.0")

        def counting(url):
            downloaded.append(url)
            return handler(url)

        self._patch_http(counting)
        firmware_fetch.fetch_latest(self.cache_root)
        # Only the API call and the manifest asset: every image already matched.
        self.assertEqual(2, len(downloaded), downloaded)


if __name__ == "__main__":
    unittest.main(verbosity=2)
