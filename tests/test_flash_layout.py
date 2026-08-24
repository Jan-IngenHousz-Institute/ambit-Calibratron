"""read_flash_layout is the integrity gate in front of esptool.

The firmware contract split in two on 2026-08-24: image *integrity* (every
manifest-listed file present, with the manifest's size and sha256) is enforced
here and is non-negotiable - a truncated bootloader at 0x0 leaves the device
dead until recovered. Release *provenance* (firmware_fetch.is_complete) is a
traceability property of the calibration record and deliberately no longer
gates flashing, so a files-complete local build is a legitimate source.
"""

import hashlib
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import helpers


IMAGES = [("bootloader.bin", "0x0"), ("partitions.bin", "0x8000"),
          ("boot_app0.bin", "0xe000"), ("ambit-fw-v9.9.9.bin", "0x10000")]


def make_firmware_folder(tmp_path, *, mutate=None):
    """A files-complete firmware folder; ``mutate`` edits entries pre-write."""
    entries = []
    for name, offset in IMAGES:
        body = name.encode() * 40
        (tmp_path / name).write_bytes(body)
        entries.append({"file": name, "offset": offset, "size": len(body),
                        "sha256": hashlib.sha256(body).hexdigest()})
    if mutate:
        mutate(entries)
    (tmp_path / "manifest.json").write_text(
        json.dumps({"name": "ambit-iot", "chip": "esp32c3", "version": "9.9.9",
                    "flash": entries, "ota": {"file": "ambit-fw-v9.9.9.bin"}}),
        encoding="utf-8")
    return tmp_path


def test_a_complete_folder_yields_the_layout_in_offset_order(tmp_path):
    folder = make_firmware_folder(tmp_path)
    layout = helpers.read_flash_layout(folder)
    assert layout == [(offset, name) for name, offset in IMAGES]


def test_a_missing_image_is_a_flat_refusal(tmp_path):
    folder = make_firmware_folder(tmp_path)
    (folder / "boot_app0.bin").unlink()
    with pytest.raises(FileNotFoundError, match="boot_app0.bin"):
        helpers.read_flash_layout(folder)


def test_a_truncated_image_never_reaches_esptool(tmp_path):
    folder = make_firmware_folder(tmp_path)
    body = (folder / "bootloader.bin").read_bytes()
    (folder / "bootloader.bin").write_bytes(body[:-1])
    with pytest.raises(RuntimeError, match="size .* disagrees"):
        helpers.read_flash_layout(folder)


def test_a_swapped_image_of_the_right_size_is_caught_by_sha256(tmp_path):
    folder = make_firmware_folder(tmp_path)
    body = bytearray((folder / "partitions.bin").read_bytes())
    body[0] ^= 0xFF
    (folder / "partitions.bin").write_bytes(bytes(body))
    with pytest.raises(RuntimeError, match="sha256 disagrees"):
        helpers.read_flash_layout(folder)


def test_an_unverifiable_manifest_entry_is_refused_not_trusted(tmp_path):
    # No size/sha256 means integrity cannot be proven, which must read as
    # "refuse", never as "assume fine".
    folder = make_firmware_folder(
        tmp_path, mutate=lambda entries: entries[0].pop("sha256"))
    with pytest.raises(RuntimeError, match="cannot be verified"):
        helpers.read_flash_layout(folder)
