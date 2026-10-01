"""Post-flash verification at the local firmware worker boundary."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import calibratron_gui as gui
import firmware_fetch
import helpers
import run_Calibratron as rc


def setup_verified_release(monkeypatch, readback, flash_result=True):
    app = gui.CalibratronGUI.__new__(gui.CalibratronGUI)
    provenance = {"tag": "v1.1.3-rc1", "immutable": True}
    monkeypatch.setattr(gui, "check_firmware_folder", lambda _folder: {
        "verified": True, "version": "1.1.3-rc1",
    })
    monkeypatch.setattr(firmware_fetch, "release_provenance",
                        lambda _folder: provenance)
    flash_calls = []
    monkeypatch.setattr(helpers, "flash_ambit_firmware", lambda folder: (
        flash_calls.append(folder), flash_result)[1])
    monkeypatch.setattr(rc, "_detect_ambit_version", lambda: readback)
    monkeypatch.setattr(gui.time, "sleep", lambda _seconds: None)
    invalidations = []
    monkeypatch.setattr(helpers, "_invalidate_port_cache",
                        lambda: invalidations.append(True))
    return app, provenance, flash_calls, invalidations


def test_local_flash_accepts_numeric_device_version_for_prerelease(monkeypatch):
    app, provenance, flash_calls, invalidations = setup_verified_release(
        monkeypatch, readback="1.1.3")

    result = app._flash_local("verified-cache-entry", "1.1.2", force=False)

    assert result == (0, provenance)
    assert flash_calls == ["verified-cache-entry"]
    assert invalidations == [True]


@pytest.mark.parametrize("readback", ["1.1.2", None, "unparseable"])
def test_local_flash_rejects_mismatched_or_unreadable_readback(monkeypatch, readback):
    app, _provenance, flash_calls, invalidations = setup_verified_release(
        monkeypatch, readback=readback)

    with pytest.raises(RuntimeError, match="readback mismatch"):
        app._flash_local("verified-cache-entry", "1.1.2", force=False)

    assert flash_calls == ["verified-cache-entry"]
    assert invalidations == [True]


def test_local_flash_rejects_false_flash_result_without_readback(monkeypatch):
    app, _provenance, flash_calls, invalidations = setup_verified_release(
        monkeypatch, readback="1.1.3", flash_result=False)
    readbacks = []
    monkeypatch.setattr(rc, "_detect_ambit_version",
                        lambda: readbacks.append(True))

    with pytest.raises(RuntimeError, match="did not complete"):
        app._flash_local("verified-cache-entry", "1.1.2", force=False)

    assert flash_calls == ["verified-cache-entry"]
    assert readbacks == []
    assert invalidations == []
