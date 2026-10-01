"""Writable storage outside an installed application bundle."""
import os
import sys
from pathlib import Path


def data_dir():
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    if override := os.environ.get("CALIBRATRON_DATA_DIR"):
        return Path(override).expanduser().resolve()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "Calibratron"


def configure_tls():
    """Let stdlib urllib and MQTT use packaged roots on clean installations.

    Retain an explicit operator CA override (for example an enterprise proxy).
    """
    if getattr(sys, "frozen", False):
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
