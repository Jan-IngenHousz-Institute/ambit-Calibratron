"""Headless integration checks for the extracted GUI worker."""

import json
import queue
import sys
from pathlib import Path

import pytest

# pytest may import this nested test directory without adding the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import calibratron_gui as gui
import firmware_fetch
import helpers
import run_Calibratron as rc


class FakeAmbitInfo:
    IsValid = True
    name = b"bench-unit"
    FW = b"1.1.2"
    MAC = "aa:bb:cc:dd:ee:ff"
    light_slope = 10.0
    act_led_coeff = 0.02

    def to_dict(self):
        return {"name": self.name.decode(), "FW": self.FW.decode(),
                "MAC": self.MAC}


@pytest.fixture
def fake_bench(monkeypatch):
    """Replace device boundaries while retaining the real calibration backend."""
    currents = {"value": 0.0}
    led = {"value": 0}
    calls = {"current": [], "led": [], "arrun": []}

    monkeypatch.setattr(gui, "discover_roles", lambda: {
        "ambit": "fake-ambit", "par_ref": "fake-par", "emit_led": "fake-led",
        "dc": "fake-dc",
    })
    monkeypatch.setattr(helpers, "ambit_reboot", lambda _port: FakeAmbitInfo())
    monkeypatch.setattr(helpers, "set_current", lambda port, current: (
        calls["current"].append((port, current)), currents.__setitem__("value", current)))
    monkeypatch.setattr(helpers, "get_par_MP", lambda port: (
        currents["value"] * 100 if port == "fake-par" else led["value"] * 100))
    monkeypatch.setattr(helpers, "get_spec_raw_MP", lambda _port: [1] * 10)
    monkeypatch.setattr(helpers, "get_par_AMB", lambda *_a, **_kw: (
        currents["value"] * 10, [1] * 10))
    monkeypatch.setattr(helpers, "record_arrun_AMB", lambda _port, actinic=0: (
        calls["arrun"].append(actinic), {"actinic": actinic})[1])
    monkeypatch.setattr(helpers, "set_ambit_led", lambda _port, setting: (
        calls["led"].append(setting), led.__setitem__("value", setting)))
    monkeypatch.setattr(rc.time, "sleep", lambda _seconds: None)
    return calls


def test_worker_uses_main_calibration_results_without_hardware_or_network(
        fake_bench, monkeypatch, tmp_path):
    """Exercise the extracted worker against the production calibration APIs."""
    app = gui.CalibratronGUI.__new__(gui.CalibratronGUI)
    app.run_options = {"par": True, "led": True, "flash": False, "force": False,
                       "upload": False, "publish": False, "mode": "github",
                       "folder": "", "client": None}
    app._ask_device_name = lambda _current: None
    written = []
    real_save = rc.save_payload

    def save(payload, mac=None):
        path = real_save(payload, mac=mac, directory=str(tmp_path))
        written.append(path)
        return path

    monkeypatch.setattr(rc, "save_payload", save)
    while True:
        try:
            gui._LOG_QUEUE.get_nowait()
        except queue.Empty:
            break

    app._run_device_worker()

    assert written and len(written) == 1
    payload = json.loads(Path(written[0]).read_text(encoding="utf-8"))
    result = payload["sample"][0]["set"][0]
    par = result["PAR_SENSOR_CALIBRATION"]
    led = result["LED_CALIBRATION"]
    assert par["quality"]["passed"] and par["slope"] == pytest.approx(10)
    assert par["x"] == pytest.approx([value * 10 for value in rc.PAR_CAL_CURRENTS])
    assert led["quality"]["passed"] and led["slope"] == pytest.approx(0.01)
    assert led["x"] == pytest.approx([value * 100 for value in rc.LED_CAL_SETTINGS])
    assert result["FIRMWARE_RELEASE_PROVENANCE"] is None
    assert payload["device_id"] == "aa:bb:cc:dd:ee:ff"
    assert fake_bench["current"][-1] == ("fake-dc", 0.0)
    assert fake_bench["led"] == rc.LED_CAL_SETTINGS
    assert Path(written[0]).parent == tmp_path
    assert Path(written[0]).name.endswith("_aa_bb_cc_dd_ee_ff.json")
    assert not any(char in Path(written[0]).name for char in '<>:"/\\|?*')
    messages = []
    while True:
        try:
            messages.append(gui._LOG_QUEUE.get_nowait())
        except queue.Empty:
            break
    assert any(kind == "session" and row["mac"] == "aa:bb:cc:dd:ee:ff"
               for kind, row in messages)


def test_local_firmware_without_provenance_is_rejected_before_flash(tmp_path, monkeypatch):
    folder = tmp_path / "unproven-release"
    folder.mkdir()
    manifest = {
        "name": "ambit-iot", "version": "1.1.3-rc1", "chip": "esp32c3",
        "flash": [], "ota": {"file": "ambit-fw-v1.1.3-rc1.bin"},
    }
    import hashlib
    for name, offset in (("bootloader.bin", "0x0"), ("partitions.bin", "0x8000"),
                         ("boot_app0.bin", "0xe000"),
                         ("ambit-fw-v1.1.3-rc1.bin", "0x10000")):
        body = (name + " test bytes").encode()
        (folder / name).write_bytes(body)
        manifest["flash"].append({"file": name, "offset": offset,
                                  "size": len(body),
                                  "sha256": hashlib.sha256(body).hexdigest()})
    (folder / firmware_fetch.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert gui.check_firmware_folder(folder)["files_ok"] is True
    assert gui.check_firmware_folder(folder)["verified"] is False
    attempted = []
    monkeypatch.setattr(helpers, "flash_ambit_firmware",
                        lambda *_a, **_kw: attempted.append(True))
    app = gui.CalibratronGUI.__new__(gui.CalibratronGUI)

    with pytest.raises(RuntimeError, match="complete verified immutable"):
        app._flash_local(str(folder), "1.1.2", force=False)

    assert attempted == []


def test_save_payload_sanitizes_unsafe_mac_as_filename_component(tmp_path):
    path = Path(rc.save_payload({"device_id": "../../unsafe:name"},
                                directory=str(tmp_path)))

    assert path.parent == tmp_path
    assert path.name.endswith("_.._.._unsafe_name.json")
    assert not any(char in path.name for char in '<>:"/\\|?*')
    assert json.loads(path.read_text(encoding="utf-8"))["device_id"] == "../../unsafe:name"


def test_local_firmware_downgrade_is_skipped_even_when_force_is_set(monkeypatch):
    app = gui.CalibratronGUI.__new__(gui.CalibratronGUI)
    monkeypatch.setattr(gui, "check_firmware_folder", lambda _folder: {
        "verified": True, "version": "1.1.2",
    })
    monkeypatch.setattr(firmware_fetch, "release_provenance", lambda _folder: {
        "tag": "v1.1.2", "immutable": True,
    })
    attempted = []
    monkeypatch.setattr(helpers, "flash_ambit_firmware",
                        lambda *_a, **_kw: attempted.append(True))

    status, provenance = app._flash_local("verified-cache-entry", "1.1.3", force=True)

    assert status == 0
    assert provenance["tag"] == "v1.1.2"
    assert attempted == []
