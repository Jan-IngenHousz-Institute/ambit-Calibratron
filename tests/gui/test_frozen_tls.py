"""Frozen certificate configuration and launcher smoke-mode checks."""

import builtins
import os
import runpy
import ssl
import sys
import types
from pathlib import Path

import certifi

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import runtime_paths


def test_frozen_default_ssl_context_has_roots_without_system_cert_dir(monkeypatch):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", "")

    runtime_paths.configure_tls()
    context = ssl.create_default_context()

    assert os.environ["SSL_CERT_FILE"] == certifi.where()
    assert context.cert_store_stats()["x509_ca"] > 0


def test_frozen_tls_preserves_operator_certificate_override(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    override = str(tmp_path / "operator-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", override)

    runtime_paths.configure_tls()

    assert os.environ["SSL_CERT_FILE"] == override


def test_source_runtime_does_not_set_a_frozen_certificate_default(monkeypatch):
    monkeypatch.setattr(runtime_paths.sys, "frozen", False, raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)

    runtime_paths.configure_tls()

    assert "SSL_CERT_FILE" not in os.environ


def test_launcher_configures_tls_before_gui_import_and_smoke_loads_cafile(
        monkeypatch, tmp_path):
    events = []
    cafile = str(tmp_path / "bundled-ca.pem")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["launcher.py", "--smoke-test"])
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("SSL_CERT_DIR", "")

    runtime_stub = types.ModuleType("runtime_paths")

    def configure_tls():
        events.append("configure_tls")
        os.environ.setdefault("SSL_CERT_FILE", cafile)

    runtime_stub.configure_tls = configure_tls
    runtime_stub.data_dir = lambda: tmp_path / "state"
    monkeypatch.setitem(sys.modules, "runtime_paths", runtime_stub)

    class FakeRoot:
        def withdraw(self):
            pass

        def update(self):
            pass

        def destroy(self):
            pass

    tk_stub = types.ModuleType("tkinter")
    tk_stub.Tk = FakeRoot
    monkeypatch.setitem(sys.modules, "tkinter", tk_stub)

    gui_stub = types.ModuleType("calibratron_gui")
    gui_stub.__file__ = str(tmp_path / "bundle" / "calibratron_gui.py")
    gui_stub.CalibratronGUI = lambda _root, auto_sign_in: events.append(
        ("gui", auto_sign_in))
    monkeypatch.setitem(sys.modules, "calibratron_gui", gui_stub)

    helpers_stub = types.ModuleType("helpers")
    helpers_stub.__file__ = str(tmp_path / "bundle" / "helpers.py")
    helpers_stub.esptool_command = lambda: [sys.executable, "--esptool"]
    monkeypatch.setitem(sys.modules, "helpers", helpers_stub)

    class FakeSSLContext:
        def __init__(self, protocol):
            events.append(("ssl-context", protocol))
            self.loaded = None

        def load_verify_locations(self, *, cafile):
            self.loaded = cafile
            events.append(("load-cafile", cafile))

        def cert_store_stats(self):
            return {"x509_ca": 1}

    monkeypatch.setattr(ssl, "SSLContext", FakeSSLContext)
    subprocess_stub = types.ModuleType("subprocess")
    subprocess_stub.run = lambda *args, **kwargs: events.append(
        ("subprocess", args, kwargs))
    monkeypatch.setitem(sys.modules, "subprocess", subprocess_stub)

    real_import = builtins.__import__

    def track_import(name, *args, **kwargs):
        if name in {"subprocess", "ssl", "tkinter", "calibratron_gui", "helpers"}:
            events.append(("import", name))
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", track_import)
    launcher = Path(__file__).resolve().parents[2] / "packaging" / "launcher.py"
    runpy.run_path(str(launcher), run_name="__main__")

    configured_at = events.index("configure_tls")
    for module in ("subprocess", "ssl", "tkinter", "calibratron_gui", "helpers"):
        imported_at = events.index(("import", module))
        assert configured_at < imported_at
    assert ("load-cafile", cafile) in events, repr(events)
