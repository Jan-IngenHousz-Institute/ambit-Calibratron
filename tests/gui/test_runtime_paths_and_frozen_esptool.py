"""Installed-app path and flasher command checks with no device access."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import firmware_fetch
import helpers
import runtime_paths


def test_data_dir_uses_frozen_linux_user_data_location(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(runtime_paths.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert runtime_paths.data_dir() == tmp_path / "xdg" / "Calibratron"


def test_data_dir_honors_installed_app_override(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setenv("CALIBRATRON_DATA_DIR", str(tmp_path / "state"))

    assert runtime_paths.data_dir() == (tmp_path / "state").resolve()


def test_frozen_esptool_command_and_subprocess_argv_are_app_mode(monkeypatch,
                                                                tmp_path):
    monkeypatch.setattr(helpers.sys, "frozen", True, raising=False)
    monkeypatch.setattr(helpers.sys, "executable", "/opt/calibratron/calibratron")
    monkeypatch.setattr(firmware_fetch, "is_complete", lambda _folder: True)
    monkeypatch.setattr(helpers, "read_flash_layout", lambda _folder: [
        ("0x0", "bootloader.bin"), ("0x10000", "app.bin")])
    invoked = []

    class Result:
        returncode = 0

    def fake_run(args, *, cwd):
        invoked.append((args, cwd))
        return Result()

    monkeypatch.setattr(helpers.subprocess, "run", fake_run)

    assert helpers.esptool_command() == ["/opt/calibratron/calibratron", "--esptool"]
    helpers.flash_ambit("COM7", tmp_path)

    args, cwd = invoked[0]
    assert args[:2] == ["/opt/calibratron/calibratron", "--esptool"]
    assert args[2:4] == ["--chip", "esp32c3"]
    assert args[-4:] == ["0x0", "bootloader.bin", "0x10000", "app.bin"]
    assert cwd == str(tmp_path)
