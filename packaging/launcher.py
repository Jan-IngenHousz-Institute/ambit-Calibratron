"""Frozen GUI entry point, including a subprocess mode for bundled esptool."""
import os
import sys
from pathlib import Path
from runtime_paths import configure_tls

configure_tls()

# Windowed builds have no standard streams. esptool expects writable streams.
for name in ("stdout", "stderr"):
    if getattr(sys, name) is None:
        setattr(sys, name, open(os.devnull, "w"))

if __name__ == "__main__":
    if sys.argv[1:2] == ["--esptool"]:
        import esptool
        esptool.main(sys.argv[2:])
    elif sys.argv[1:2] == ["--smoke-test"]:
        # Exercise imports, Tcl/Tk, and the actual bundled flasher without USB
        # probing, network requests, or running a calibration.
        import subprocess
        import ssl
        import tkinter as tk
        import calibratron_gui
        import helpers
        import runtime_paths

        # Explicitly load only the bundled/overridden roots, without relying
        # on this runner's system certificate store.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cafile=os.environ["SSL_CERT_FILE"])
        assert context.cert_store_stats()["x509_ca"] > 0

        root = tk.Tk()
        root.withdraw()
        calibratron_gui.CalibratronGUI(root, auto_sign_in=False)
        root.update()
        root.destroy()
        subprocess.run([*helpers.esptool_command(), "version"], check=True, timeout=30)
        assert runtime_paths.data_dir() != Path(helpers.__file__).resolve().parent
        Path("packaged-smoke-ok.txt").write_text("GUI imports, Tk and esptool passed\n")
    else:
        from runtime_paths import data_dir
        data_dir().mkdir(parents=True, exist_ok=True)
        from calibratron_gui import main
        main()
