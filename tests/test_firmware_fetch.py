"""Release-contract, cache-integrity, and offline firmware-fetch tests."""

import ast
import copy
import hashlib
import http.client
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import firmware_fetch  # noqa: E402


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _repository():
    return {
        "full_name": firmware_fetch.FIRMWARE_REPO,
        "private": False,
        "visibility": "public",
        "archived": False,
        "disabled": False,
    }


def _make_contract(version="1.1.3-rc1"):
    images = {
        "bootloader.bin": b"bootloader-" + version.encode(),
        "partitions.bin": b"partitions-" + version.encode(),
        "boot_app0.bin": b"boot_app0-" + version.encode(),
        f"ambit-fw-v{version}.bin": b"app-" + version.encode(),
    }
    offsets = {
        "bootloader.bin": "0x0",
        "partitions.bin": "0x8000",
        "boot_app0.bin": "0xe000",
        f"ambit-fw-v{version}.bin": "0x10000",
    }
    manifest = {
        "name": "ambit-iot",
        "version": version,
        "chip": "esp32c3",
        "flash": [
            {
                "file": name,
                "offset": offsets[name],
                "size": len(body),
                "sha256": _sha256(body),
            }
            for name, body in images.items()
        ],
        "ota": {"file": f"ambit-fw-v{version}.bin"},
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode()
    bodies = dict(images)
    bodies[firmware_fetch.MANIFEST_NAME] = manifest_bytes
    tag = f"v{version}"
    release = {
        "id": 1,
        "tag_name": tag,
        "draft": False,
        "prerelease": "-" in version,
        "published_at": "2026-08-05T20:14:20Z",
        "immutable": True,
        "assets": [
            {
                "id": index,
                "name": name,
                "state": "uploaded",
                "size": len(body),
                "digest": f"sha256:{_sha256(body)}",
                "content_type": (
                    "application/json" if name == firmware_fetch.MANIFEST_NAME
                    else "application/octet-stream"
                ),
                "browser_download_url": firmware_fetch._canonical_asset_url(tag, name),
            }
            for index, (name, body) in enumerate(bodies.items(), start=1)
        ],
    }
    return _repository(), release, manifest, manifest_bytes, bodies


def _write_cache_entry(cache_root, version="1.1.3-rc1", *, tamper=None,
                       omit_provenance=False):
    repository, release, _manifest, manifest_bytes, bodies = _make_contract(version)
    version_dir = Path(cache_root) / version
    version_dir.mkdir(parents=True, exist_ok=True)
    for name, body in bodies.items():
        if name == tamper:
            body += b"-tampered"
        (version_dir / name).write_bytes(body)
    if not omit_provenance:
        (version_dir / firmware_fetch.RELEASE_METADATA_NAME).write_text(
            json.dumps({"repository": repository, "release": release}), encoding="utf-8"
        )
    return version_dir


class _TempCache(unittest.TestCase):
    def setUp(self):
        self.cache_root = Path(tempfile.mkdtemp(prefix="fwcache-"))
        self.addCleanup(shutil.rmtree, self.cache_root, ignore_errors=True)


class TestProductionContractFixtures(unittest.TestCase):
    def test_captured_anonymous_v1_1_3_rc1_contract(self):
        repository = json.loads((FIXTURES / "ambit-repository.json").read_text())
        release = json.loads((FIXTURES / "ambit-v1.1.3-rc1-release.json").read_text())
        manifest_path = FIXTURES / "ambit-v1.1.3-rc1-manifest.json"
        manifest = json.loads(manifest_path.read_text())

        firmware_fetch.validate_public_repository(repository)
        self.assertIs(release, firmware_fetch.select_release([release]))
        tag, assets = firmware_fetch.validate_release(release)
        entries = firmware_fetch.manifest_entries(manifest, tag=tag, assets=assets)

        self.assertEqual("v1.1.3-rc1", tag)
        self.assertEqual(4, len(entries))
        self.assertEqual(849, manifest_path.stat().st_size)
        self.assertEqual(
            "bc6ecd522c768e97d770cb2c552761de33f7adddfe78dd42274377452849e7df",
            firmware_fetch.sha256_file(manifest_path),
        )

    def test_future_stable_is_generic_default_but_old_stable_is_not(self):
        _repo, approved, *_ = _make_contract("1.1.3-rc1")
        _repo, old_stable, *_ = _make_contract("1.1.2")
        _repo, future_stable, *_ = _make_contract("1.1.4")
        self.assertIs(future_stable, firmware_fetch.select_release(
            [approved, old_stable, future_stable]
        ))
        self.assertIs(approved, firmware_fetch.select_release([approved, old_stable]))

    def test_ineligible_newest_stable_is_skipped(self):
        _repo, approved, *_ = _make_contract("1.1.3-rc1")
        _repo, mutable, *_ = _make_contract("1.1.4")
        mutable["immutable"] = False
        self.assertIs(approved, firmware_fetch.select_release([approved, mutable]))

    def test_unapproved_prerelease_is_never_selected(self):
        _repo, other, *_ = _make_contract("1.1.4-rc1")
        with self.assertRaisesRegex(ValueError, "approved prerelease"):
            firmware_fetch.select_release([other])


class TestReleaseAndManifestValidation(unittest.TestCase):
    def setUp(self):
        self.repository, self.release, self.manifest, *_ = _make_contract()

    def test_private_repository_is_rejected(self):
        self.repository["private"] = True
        with self.assertRaisesRegex(ValueError, "not publicly visible"):
            firmware_fetch.validate_public_repository(self.repository)

    def test_draft_unpublished_or_mutable_release_is_rejected(self):
        for field, value, message in (
            ("draft", True, "draft"),
            ("published_at", None, "not published"),
            ("immutable", False, "mutable"),
        ):
            with self.subTest(field=field):
                release = copy.deepcopy(self.release)
                release[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    firmware_fetch.validate_release(release)

    def test_missing_or_noncanonical_rest_digest_is_rejected(self):
        for digest in (None, "sha256:" + "A" * 64, "md5:" + "0" * 32):
            with self.subTest(digest=digest):
                release = copy.deepcopy(self.release)
                release["assets"][0]["digest"] = digest
                with self.assertRaises(ValueError):
                    firmware_fetch.validate_release(release)

    def test_noncanonical_asset_url_is_rejected(self):
        release = copy.deepcopy(self.release)
        release["assets"][0]["browser_download_url"] = "https://example.invalid/fw.bin"
        with self.assertRaisesRegex(ValueError, "non-canonical"):
            firmware_fetch.validate_release(release)

    def test_unrelated_extra_release_assets_are_ignored(self):
        release = copy.deepcopy(self.release)
        release["assets"].append({
            "name": "sbom.json",
            "content_type": "application/spdx+json",
        })
        tag, assets = firmware_fetch.validate_release(release)
        self.assertEqual("v1.1.3-rc1", tag)
        self.assertNotIn("sbom.json", assets)

    def test_manifest_rejects_missing_sha_size_wrong_chip_and_unsafe_path(self):
        _tag, assets = firmware_fetch.validate_release(self.release)
        mutations = (
            (lambda m: m["flash"][0].pop("sha256"), "lowercase hexadecimal"),
            (lambda m: m["flash"][0].pop("size"), "positive integer"),
            (lambda m: m.__setitem__("chip", "esp32"), "esp32c3"),
            (lambda m: m["flash"][0].__setitem__("file", "../bootloader.bin"), "unsafe"),
            (lambda m: m.__setitem__("version", "../1.1.3"), "unsafe"),
        )
        for mutate, message in mutations:
            with self.subTest(message=message):
                manifest = copy.deepcopy(self.manifest)
                mutate(manifest)
                with self.assertRaisesRegex(ValueError, message):
                    firmware_fetch.manifest_entries(
                        manifest, tag=self.release["tag_name"], assets=assets
                    )

    def test_manifest_and_rest_size_or_digest_disagreement_is_rejected(self):
        _tag, assets = firmware_fetch.validate_release(self.release)
        for field, value, message in (
            ("size", self.manifest["flash"][0]["size"] + 1, "size disagreement"),
            ("sha256", "0" * 64, "digest disagreement"),
        ):
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                manifest["flash"][0][field] = value
                with self.assertRaisesRegex(ValueError, message):
                    firmware_fetch.manifest_entries(
                        manifest, tag=self.release["tag_name"], assets=assets
                    )


class TestCacheCompleteness(_TempCache):
    def test_complete_proven_cache_is_accepted(self):
        self.assertTrue(firmware_fetch.is_complete(_write_cache_entry(self.cache_root)))

    def test_release_provenance_records_exact_manifest_and_flash_assets(self):
        version_dir = _write_cache_entry(self.cache_root)
        proof = firmware_fetch.release_provenance(version_dir)
        self.assertEqual(firmware_fetch.FIRMWARE_REPO, proof["repository"])
        self.assertEqual("v1.1.3-rc1", proof["tag"])
        self.assertTrue(proof["immutable"])
        self.assertEqual(4, len(proof["flash"]))
        self.assertEqual(
            _sha256((version_dir / firmware_fetch.MANIFEST_NAME).read_bytes()),
            proof["manifest"]["sha256"],
        )
        for entry in proof["flash"]:
            self.assertEqual(
                _sha256((version_dir / entry["file"]).read_bytes()),
                entry["sha256"],
            )

    def test_cache_without_release_provenance_fails_closed(self):
        path = _write_cache_entry(self.cache_root, omit_provenance=True)
        self.assertFalse(firmware_fetch.is_complete(path))

    def test_integrity_is_independent_of_current_selection_policy(self):
        path = _write_cache_entry(self.cache_root, "1.1.2")
        self.assertTrue(firmware_fetch.is_complete(path))

    def test_tampered_manifest_or_image_fails_closed(self):
        for name in (firmware_fetch.MANIFEST_NAME, "bootloader.bin"):
            with self.subTest(name=name):
                root = self.cache_root / name.replace(".", "-")
                path = _write_cache_entry(root, tamper=name)
                self.assertFalse(firmware_fetch.is_complete(path))

    def test_newest_cached_ignores_corrupt_and_sorts_versions(self):
        _write_cache_entry(self.cache_root, "1.1.3-rc1")
        _write_cache_entry(self.cache_root, "1.1.4", tamper="partitions.bin")
        version, path = firmware_fetch.newest_cached(self.cache_root)
        self.assertEqual("1.1.3-rc1", version)
        self.assertEqual("1.1.3-rc1", path.name)


class TestFetchLatest(_TempCache):
    def _patch_http(self, handler):
        original = firmware_fetch._http_get
        firmware_fetch._http_get = lambda url, accept=None: handler(url)
        self.addCleanup(setattr, firmware_fetch, "_http_get", original)

    def _release_handler(self, version="1.1.3-rc1", *, corrupt=None):
        repository, release, _manifest, _manifest_bytes, bodies = _make_contract(version)
        urls = {asset["browser_download_url"]: asset["name"] for asset in release["assets"]}

        def handler(url):
            if url == firmware_fetch.REPOSITORY_URL:
                return json.dumps(repository).encode()
            if url == firmware_fetch.RELEASES_URL:
                return json.dumps([release]).encode()
            name = urls[url]
            body = bodies[name]
            return body + b"-flipped-bit" if name == corrupt else body

        return handler

    def test_downloads_and_retains_verified_release_provenance(self):
        self._patch_http(self._release_handler())
        version, path = firmware_fetch.fetch_latest(self.cache_root)
        self.assertEqual("1.1.3-rc1", version)
        self.assertTrue(firmware_fetch.is_complete(path))
        metadata = firmware_fetch.read_release_metadata(path)
        self.assertFalse(metadata["repository"]["private"])
        self.assertTrue(metadata["release"]["immutable"])

    def test_download_size_or_sha_mismatch_leaves_cache_incomplete(self):
        self._patch_http(self._release_handler(corrupt="bootloader.bin"))
        with self.assertRaisesRegex(RuntimeError, "size mismatch"):
            firmware_fetch.fetch_latest(self.cache_root)
        self.assertFalse(firmware_fetch.is_complete(self.cache_root / "1.1.3-rc1"))

    def test_offline_fallback_uses_newest_complete_proven_cache(self):
        path = _write_cache_entry(self.cache_root)
        self._patch_http(lambda _url: (_ for _ in ()).throw(
            urllib.error.URLError("bench has no uplink")
        ))
        self.assertEqual(("1.1.3-rc1", path), firmware_fetch.fetch_latest(self.cache_root))

    def test_offline_cached_target_cannot_downgrade_newer_device(self):
        _write_cache_entry(self.cache_root)
        self._patch_http(lambda _url: (_ for _ in ()).throw(
            urllib.error.URLError("bench has no uplink")
        ))
        target, _path = firmware_fetch.fetch_latest(self.cache_root)
        self.assertEqual("newer", firmware_fetch.flash_decision("1.1.4", target))

    def test_offline_without_proven_cache_raises(self):
        _write_cache_entry(self.cache_root, omit_provenance=True)
        self._patch_http(lambda _url: (_ for _ in ()).throw(
            urllib.error.URLError("bench has no uplink")
        ))
        with self.assertRaisesRegex(RuntimeError, "No usable firmware"):
            firmware_fetch.fetch_latest(self.cache_root)

    def test_network_read_and_online_contract_failures_use_proven_cache(self):
        path = _write_cache_entry(self.cache_root)
        failures = (
            TimeoutError("read timed out"),
            ConnectionResetError("reset during read"),
            http.client.IncompleteRead(b"partial", 100),
            RuntimeError("online release failed validation"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                firmware_fetch, "_http_get", side_effect=failure,
            ):
                self.assertEqual(
                    ("1.1.3-rc1", path), firmware_fetch.fetch_latest(self.cache_root)
                )

    def test_http_404_description_is_clear(self):
        error = urllib.error.HTTPError("https://example.invalid", 404, "Not Found", {}, None)
        try:
            self.assertIn("not visible", firmware_fetch._describe_url_error(error))
        finally:
            error.close()

    def test_existing_verified_images_are_not_redownloaded(self):
        _write_cache_entry(self.cache_root)
        calls = []
        handler = self._release_handler()

        def counting(url):
            calls.append(url)
            return handler(url)

        self._patch_http(counting)
        firmware_fetch.fetch_latest(self.cache_root)
        self.assertEqual(
            [firmware_fetch.REPOSITORY_URL, firmware_fetch.RELEASES_URL,
             firmware_fetch._canonical_asset_url("v1.1.3-rc1", "manifest.json")],
            calls,
        )

    def test_token_is_sent_only_to_api_host_not_public_assets(self):
        asset_url = firmware_fetch._canonical_asset_url(
            "v1.1.3-rc1", "manifest.json"
        )
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": "test-secret"}):
            api_request = firmware_fetch._request(
                firmware_fetch.REPOSITORY_URL, "application/vnd.github+json"
            )
            asset_request = firmware_fetch._request(asset_url, "application/octet-stream")
        self.assertEqual("Bearer test-secret", api_request.get_header("Authorization"))
        self.assertIsNone(asset_request.get_header("Authorization"))


class TestDeviceVersionPolicy(unittest.TestCase):
    def test_prerelease_is_device_equivalent_to_numeric_core(self):
        self.assertEqual("1.1.3", firmware_fetch.device_visible_version("1.1.3-rc1"))
        self.assertEqual(0, firmware_fetch.compare_device_versions("1.1.3", "1.1.3-rc1"))
        self.assertEqual("equivalent", firmware_fetch.flash_decision("1.1.3", "1.1.3-rc1"))

    def test_normal_flow_is_strictly_upgrade_only(self):
        self.assertEqual("upgrade", firmware_fetch.flash_decision("1.1.2", "1.1.3-rc1"))
        self.assertEqual("newer", firmware_fetch.flash_decision("1.1.4", "1.1.3-rc1"))
        self.assertEqual("unknown", firmware_fetch.flash_decision(None, "1.1.3-rc1"))

    def test_force_and_downgrade_are_separate_explicit_controls(self):
        self.assertEqual("reflash", firmware_fetch.flash_decision(
            "1.1.3", "1.1.3-rc1", force=True
        ))
        self.assertEqual("newer", firmware_fetch.flash_decision(
            "1.1.4", "1.1.3-rc1", force=True
        ))
        self.assertEqual("downgrade", firmware_fetch.flash_decision(
            "1.1.4", "1.1.3-rc1", allow_downgrade=True
        ))
        self.assertEqual("recovery", firmware_fetch.flash_decision(
            None, "1.1.3-rc1", force=True
        ))
        self.assertEqual("recovery", firmware_fetch.flash_decision(
            None, "1.1.3-rc1", force=True, allow_downgrade=True
        ))


class TestBenchContractRegression(unittest.TestCase):
    """Guard the bench I/O contract the tier-3 calibration depends on.

    These are the symbols a release must not silently lose: the MiniPAR raw
    spectrum, the reset-free serial open, cached role discovery, the ADPD trace
    with its pulse-LED zeroing, the integrity-gated flash, and the release
    policy calls in the runner.
    """

    def test_bench_io_and_flash_policy_symbols_remain_present(self):
        root = Path(__file__).resolve().parent.parent
        helpers_tree = ast.parse((root / "helpers.py").read_text(encoding="utf-8"))
        runner_tree = ast.parse((root / "run_calibratron.py").read_text(encoding="utf-8"))
        helper_defs = {
            node.name: node for node in ast.walk(helpers_tree)
            if isinstance(node, ast.FunctionDef)
        }
        helper_globals = {
            target.id for node in helpers_tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
        }
        runner_constants = {
            node.value for node in ast.walk(runner_tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        runner_attributes = {
            node.attr for node in ast.walk(runner_tree) if isinstance(node, ast.Attribute)
        }
        self.assertTrue({"get_spec_raw_MP", "open_serial_no_reset", "discover_roles",
                         "arrun", "zero_pulse_currents", "flash_ambit_firmware",
                         "esptool_command"} <= set(helper_defs))
        self.assertIn("HELLO_FW_RE", helper_globals)
        self.assertIn("ambit_spec_channels", runner_constants)
        self.assertTrue({"flash_decision", "release_provenance"} <= runner_attributes)
        # Flashing is gated on every image matching the manifest's size/sha256.
        self.assertTrue(any(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "read_flash_layout"
            for node in ast.walk(helper_defs["flash_ambit_firmware"])
        ))
        # Bench questions are bounded: _query carries a read timeout and retries.
        self.assertTrue({"timeout", "attempts"} <= {
            arg.arg for arg in helper_defs["_query"].args.args})
        self.assertIn("leaf", (root / "helpers.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
