"""Calibratron GUI - a tkinter front-end over run_calibratron.

One window for a whole bench session: discover the instruments, run the same
per-device pass as ``run_calibratron.main()`` (flash, name, tier-3 PAR, actinic
LED, record, publish), and keep a table of every device calibrated since the
window opened. Designed for back-to-back devices: swap the Ambit in the
fixture, type the next name, press Start again.

What it deliberately does NOT change: all calibration logic, gates and record
formats live in run_calibratron / helpers / spec_cal / quality and are called
unmodified. This file is widgets and a worker thread, nothing else.

Firmware can come from two places:
  - the GitHub release (default) - exactly run_calibratron.flash_firmware();
  - a local folder. "Check files" reports, file by file, whether the folder
    is complete: manifest.json, the four flash images with the manifest's
    sizes and sha256 digests, and release provenance. Image integrity is the
    hard gate - a folder that fails size/sha256 never reaches esptool. Release
    provenance is not: a files-complete local build flashes fine, and the
    calibration record then carries an honest ``verified: false`` provenance
    with the per-file hashes instead of the GitHub release proof.

The ADPD dark baseline step is not offered: it needs a fixture change mid-run
(see the run_calibratron module docstring) and calibrates nothing on the PAR
chain. Run it from the CLI when it is needed.

On close the downloaded firmware cache (run_calibratron.FIRMWARE_CACHE_DIR)
is deleted - releases are re-proven and re-downloaded next session, so the
bench never trusts a stale cache. A user-selected local firmware folder is
never touched.
"""

from __future__ import annotations

import os
import queue
import shutil
import sys
import threading
import time
import traceback

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ---------------------------------------------------------------------------
# stdout tee - installed BEFORE the bench modules import, so both print() and
# the helpers logger (whose handler captures sys.stdout at import time) land
# in the GUI log as well as the terminal.
# ---------------------------------------------------------------------------

_LOG_QUEUE: "queue.Queue[tuple[str, object]]" = queue.Queue()


class _Tee:
    """Forward writes to the original stream and to the GUI log queue."""

    def __init__(self, original):
        self._original = original

    def write(self, text):
        try:
            if self._original is not None:
                self._original.write(text)
        except Exception:
            pass
        if text:
            _LOG_QUEUE.put(("log", text))
        return len(text)

    def flush(self):
        try:
            if self._original is not None:
                self._original.flush()
        except Exception:
            pass

    def isatty(self):
        return False


sys.stdout = _Tee(sys.stdout)

import firmware_fetch
import helpers
import run_calibratron as rc


# ---------------------------------------------------------------------------
# Firmware folder check
# ---------------------------------------------------------------------------

_MANIFEST_NAME = getattr(firmware_fetch, "MANIFEST_NAME", "manifest.json")
_RELEASE_NAME = getattr(firmware_fetch, "RELEASE_METADATA_NAME", "release.json")


def check_firmware_folder(folder):
    """Audit a local firmware folder file by file.

    :return: dict with ``rows`` ([(filename, ok, detail), ...] for the GUI
        table), ``files_ok`` (manifest + every flash image present with the
        manifest's size and sha256), ``verified`` (full
        :func:`firmware_fetch.is_complete` provenance contract) and
        ``version`` (from the manifest, or None).
    """
    rows, files_ok, version = [], True, None
    checked = []

    manifest = firmware_fetch.read_manifest(folder)
    if manifest is None:
        rows.append((_MANIFEST_NAME, False, "missing or unreadable"))
        return {"rows": rows, "files_ok": False, "verified": False,
                "version": None, "entries": []}
    rows.append((_MANIFEST_NAME, True, "readable"))

    try:
        entries = firmware_fetch.manifest_entries(manifest)
        version = manifest.get("version")
    except (TypeError, ValueError) as exc:
        rows.append((_MANIFEST_NAME, False, f"invalid: {exc}"))
        return {"rows": rows, "files_ok": False, "verified": False,
                "version": None, "entries": []}

    for offset, name, want_size, want_sha in entries:
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            rows.append((name, False, f"MISSING (expected at {offset})"))
            files_ok = False
            continue
        size = os.path.getsize(path)
        if size != want_size:
            rows.append((name, False, f"size {size} B, manifest says {want_size} B"))
            files_ok = False
            continue
        if firmware_fetch.sha256_file(path) != want_sha:
            rows.append((name, False, "sha256 mismatch against the manifest"))
            files_ok = False
            continue
        rows.append((name, True, f"{offset}, {size} B, sha256 ok"))
        checked.append({"file": name, "offset": offset, "size": want_size,
                        "sha256": want_sha})

    if firmware_fetch.read_release_metadata(folder) is None:
        rows.append((_RELEASE_NAME, False, "missing - release provenance unproven"))
        verified = False
    else:
        verified = firmware_fetch.is_complete(folder)
        rows.append((_RELEASE_NAME, verified,
                     "provenance verified" if verified
                     else "present but fails the integrity contract"))

    return {"rows": rows, "files_ok": files_ok, "verified": verified,
            "version": version, "entries": checked}


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------

ROLES = ("ambit", "par_ref", "emit_led", "dc")
ROLE_LABELS = {
    "ambit": "Ambit (device under calibration)",
    "par_ref": "Par_REF MiniPAR (PAR reference)",
    "emit_led": "Emit_LED MiniPAR (LED reference)",
    "dc": "Kiprim DC source (lamp)",
}


class CalibratronGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Calibratron")
        root.geometry("1180x800")
        root.minsize(980, 640)

        self.ports = {role: None for role in ROLES}
        self.worker = None
        self.fw_report = None
        self.session_rows = 0

        self._build_left()
        self._build_right()
        self._build_statusbar()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._drain_queue)
        self._log_line("[gui] ready - press 'Rescan bench' to discover the instruments")

    # ---- layout -----------------------------------------------------------

    def _build_left(self):
        left = ttk.Frame(self.root, padding=8)
        left.pack(side="left", fill="y")

        # Devices
        dev = ttk.LabelFrame(left, text="Bench devices", padding=6)
        dev.pack(fill="x")
        self.device_rows = {}
        for i, role in enumerate(ROLES):
            ttk.Label(dev, text=ROLE_LABELS[role]).grid(row=i, column=0,
                                                        sticky="w", padx=(0, 8))
            var = tk.StringVar(value="—")
            lbl = ttk.Label(dev, textvariable=var, width=14, anchor="w")
            lbl.grid(row=i, column=1, sticky="w")
            self.device_rows[role] = (var, lbl)
        self.rescan_btn = ttk.Button(dev, text="Rescan bench", command=self.on_rescan)
        self.rescan_btn.grid(row=len(ROLES), column=0, columnspan=2,
                             sticky="ew", pady=(6, 0))

        # Firmware
        fw = ttk.LabelFrame(left, text="Firmware", padding=6)
        fw.pack(fill="x", pady=(8, 0))
        self.fw_mode = tk.StringVar(value="github")
        ttk.Radiobutton(fw, text="GitHub release (verified download)",
                        variable=self.fw_mode, value="github",
                        command=self._fw_mode_changed).grid(row=0, column=0,
                                                            columnspan=3, sticky="w")
        ttk.Radiobutton(fw, text="Local folder:",
                        variable=self.fw_mode, value="local",
                        command=self._fw_mode_changed).grid(row=1, column=0, sticky="w")
        self.fw_folder = tk.StringVar(value="")
        self.fw_entry = ttk.Entry(fw, textvariable=self.fw_folder, width=28)
        self.fw_entry.grid(row=2, column=0, columnspan=2, sticky="ew", pady=2)
        self.fw_browse_btn = ttk.Button(fw, text="Browse…", width=9,
                                        command=self.on_browse_folder)
        self.fw_browse_btn.grid(row=2, column=2, padx=(4, 0))
        self.fw_check_btn = ttk.Button(fw, text="Check files",
                                       command=self.on_check_folder)
        self.fw_check_btn.grid(row=3, column=0, sticky="w", pady=(2, 4))
        self.fw_verdict = tk.StringVar(value="")
        ttk.Label(fw, textvariable=self.fw_verdict, wraplength=240,
                  justify="left").grid(row=3, column=1, columnspan=2, sticky="w")

        self.fw_tree = ttk.Treeview(fw, columns=("ok", "detail"), show="tree headings",
                                    height=6)
        self.fw_tree.heading("#0", text="file")
        self.fw_tree.heading("ok", text="ok")
        self.fw_tree.heading("detail", text="detail")
        self.fw_tree.column("#0", width=130, anchor="w")
        self.fw_tree.column("ok", width=30, anchor="center")
        self.fw_tree.column("detail", width=190, anchor="w")
        self.fw_tree.grid(row=4, column=0, columnspan=3, sticky="ew")
        self._fw_mode_changed()

        # Run
        run = ttk.LabelFrame(left, text="Calibrate this device", padding=6)
        run.pack(fill="x", pady=(8, 0))
        ttk.Label(run, text="New name (blank = keep):").grid(row=0, column=0, sticky="w")
        self.name_var = tk.StringVar(value="")
        ttk.Entry(run, textvariable=self.name_var, width=18).grid(row=0, column=1,
                                                                  sticky="ew")
        self.opt_flash = tk.BooleanVar(value=rc.FLASH_FIRMWARE)
        self.opt_force = tk.BooleanVar(value=rc.FORCE_FLASH_FIRMWARE)
        self.opt_tier3 = tk.BooleanVar(value=rc.CALIBRATE_TIER3)
        self.opt_led = tk.BooleanVar(value=rc.CALIBRATE_LED)
        self.opt_upload = tk.BooleanVar(value=rc.UPLOAD_COEFFICIENTS)
        self.opt_publish = tk.BooleanVar(value=rc.PUBLISH_TO_OPENJII)
        for i, (text, var) in enumerate((
                ("Flash firmware", self.opt_flash),
                ("Force reflash of equivalent firmware", self.opt_force),
                ("Tier-3 PAR calibration (par_slope, par_intercept)", self.opt_tier3),
                ("Actinic LED calibration", self.opt_led),
                ("Write coefficients to the device", self.opt_upload),
                ("Publish the record to openJII", self.opt_publish)), start=1):
            ttk.Checkbutton(run, text=text, variable=var).grid(
                row=i, column=0, columnspan=2, sticky="w")
        self.start_btn = ttk.Button(run, text="Start calibration",
                                    command=self.on_start)
        self.start_btn.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def _build_right(self):
        right = ttk.Frame(self.root, padding=(0, 8, 8, 8))
        right.pack(side="left", fill="both", expand=True)

        paned = ttk.PanedWindow(right, orient="vertical")
        paned.pack(fill="both", expand=True)

        nb = ttk.Notebook(paned)
        paned.add(nb, weight=3)

        # -- current device tab
        cur = ttk.Frame(nb, padding=6)
        nb.add(cur, text="Current device")
        cur.columnconfigure(0, weight=1)
        cur.columnconfigure(1, weight=1)
        cur.rowconfigure(1, weight=1)

        par = ttk.LabelFrame(cur, text="Tier-3 PAR fit (par = a·par_tier2 + b)",
                             padding=6)
        par.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 6))
        par.columnconfigure(1, weight=1)
        par.rowconfigure(6, weight=1)
        self.par_vars = {}
        for i, (key, label) in enumerate((
                ("status", "Status"), ("par_slope", "par_slope"),
                ("par_intercept", "par_intercept"), ("r2", "R²"),
                ("nrmse", "NRMSE"), ("confirm", "Worst confirm error"))):
            ttk.Label(par, text=label + ":").grid(row=i, column=0, sticky="w")
            var = tk.StringVar(value="—")
            ttk.Label(par, textvariable=var).grid(row=i, column=1, sticky="w")
            self.par_vars[key] = var
        self.par_tree = ttk.Treeview(
            par, columns=("tier2", "ref", "used"), show="tree headings", height=9)
        self.par_tree.heading("#0", text="lamp A")
        self.par_tree.heading("tier2", text="par_tier2")
        self.par_tree.heading("ref", text="ref PAR")
        self.par_tree.heading("used", text="fitted")
        self.par_tree.column("#0", width=70, anchor="e")
        self.par_tree.column("tier2", width=90, anchor="e")
        self.par_tree.column("ref", width=90, anchor="e")
        self.par_tree.column("used", width=110, anchor="w")
        self.par_tree.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(6, 0))

        led = ttk.LabelFrame(cur, text="Actinic LED fit (ref PAR = c·setting)",
                             padding=6)
        led.grid(row=0, column=1, rowspan=2, sticky="nsew")
        led.columnconfigure(1, weight=1)
        led.rowconfigure(5, weight=1)
        self.led_vars = {}
        for i, (key, label) in enumerate((
                ("status", "Status"), ("coefficient", "Coefficient"),
                ("r2", "R²"), ("before", "act_led_coeff before"),
                ("after", "act_led_coeff after"))):
            ttk.Label(led, text=label + ":").grid(row=i, column=0, sticky="w")
            var = tk.StringVar(value="—")
            ttk.Label(led, textvariable=var).grid(row=i, column=1, sticky="w")
            self.led_vars[key] = var
        self.led_tree = ttk.Treeview(led, columns=("ref",), show="tree headings",
                                     height=9)
        self.led_tree.heading("#0", text="setting")
        self.led_tree.heading("ref", text="ref PAR (µmol m⁻² s⁻¹)")
        self.led_tree.column("#0", width=80, anchor="e")
        self.led_tree.column("ref", width=170, anchor="e")
        self.led_tree.grid(row=5, column=0, columnspan=2, sticky="nsew", pady=(6, 0))

        # -- session tab
        ses = ttk.Frame(nb, padding=6)
        nb.add(ses, text="Session")
        ses.columnconfigure(0, weight=1)
        ses.rowconfigure(0, weight=1)
        cols = ("name", "mac", "fw", "slope", "intercept", "par_r2",
                "led_coeff", "led_r2", "status")
        heads = ("name", "MAC", "firmware", "par_slope", "par_intercept",
                 "PAR R²", "LED coeff", "LED R²", "status")
        self.session_tree = ttk.Treeview(ses, columns=cols, show="tree headings")
        self.session_tree.heading("#0", text="#")
        self.session_tree.column("#0", width=36, anchor="e")
        for col, head in zip(cols, heads):
            self.session_tree.heading(col, text=head)
            self.session_tree.column(col, width=92, anchor="w")
        self.session_tree.grid(row=0, column=0, sticky="nsew")

        # -- log pane
        logf = ttk.LabelFrame(paned, text="Log", padding=(4, 2))
        paned.add(logf, weight=2)
        self.log = scrolledtext.ScrolledText(logf, height=10, state="disabled",
                                             font=("Consolas", 9), wrap="none")
        self.log.pack(fill="both", expand=True)

    def _build_statusbar(self):
        self.status_var = tk.StringVar(value="idle")
        bar = ttk.Label(self.root, textvariable=self.status_var, relief="sunken",
                        anchor="w", padding=(6, 2))
        bar.pack(side="bottom", fill="x")

    # ---- log plumbing -----------------------------------------------------

    def _log_line(self, text):
        _LOG_QUEUE.put(("log", text + "\n"))

    def _drain_queue(self):
        try:
            while True:
                kind, payload = _LOG_QUEUE.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload)
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "devices":
                    self._show_devices(payload)
                elif kind == "tier3":
                    self._show_tier3(payload)
                elif kind == "led":
                    self._show_led(payload)
                elif kind == "session":
                    self._add_session_row(payload)
                elif kind == "status":
                    self.status_var.set(payload)
                elif kind == "busy":
                    self._set_busy(payload)
                elif kind == "error":
                    messagebox.showerror("Calibratron", payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for btn in (self.start_btn, self.rescan_btn, self.fw_check_btn,
                    self.fw_browse_btn):
            btn.configure(state=state)

    # ---- device discovery --------------------------------------------------

    def on_rescan(self):
        self._spawn(self._rescan_worker, "scanning the bench…")

    def _rescan_worker(self):
        helpers.invalidate_port_cache()
        ports = helpers.discover_roles(helpers.DEVICE_SPECS)
        self.ports = ports
        _LOG_QUEUE.put(("devices", dict(ports)))

    def _show_devices(self, ports):
        for role in ROLES:
            var, lbl = self.device_rows[role]
            port = ports.get(role)
            var.set(port if port else "not found")
            lbl.configure(foreground="#0a7d00" if port else "#b00020")

    # ---- firmware folder ----------------------------------------------------

    def _fw_mode_changed(self):
        local = self.fw_mode.get() == "local"
        state = "normal" if local else "disabled"
        for widget in (self.fw_entry, self.fw_browse_btn, self.fw_check_btn):
            widget.configure(state=state)

    def on_browse_folder(self):
        folder = filedialog.askdirectory(title="Firmware folder (a release cache "
                                               "entry with manifest.json)")
        if folder:
            self.fw_folder.set(folder)
            self.on_check_folder()

    def on_check_folder(self):
        folder = self.fw_folder.get().strip()
        self.fw_tree.delete(*self.fw_tree.get_children())
        self.fw_report = None
        if not folder or not os.path.isdir(folder):
            self.fw_verdict.set("select a folder first")
            return
        report = check_firmware_folder(folder)
        self.fw_report = report
        for name, ok, detail in report["rows"]:
            self.fw_tree.insert("", "end", text=name, values=("✓" if ok else "✗",
                                                              detail))
        if report["verified"]:
            self.fw_verdict.set(f"complete and verified - firmware "
                                f"{report['version']}")
        elif report["files_ok"]:
            self.fw_verdict.set(f"all files present (firmware {report['version']}); "
                                f"no release provenance - flashable, and the "
                                f"record will mark the source unverified")
        else:
            self.fw_verdict.set("INCOMPLETE - see the file list")
        self._log_line(f"[gui] firmware folder check: {folder} -> "
                       f"{self.fw_verdict.get()}")

    # ---- the calibration run ------------------------------------------------

    def on_start(self):
        if self.fw_mode.get() == "local" and self.opt_flash.get():
            folder = self.fw_folder.get().strip()
            if not folder:
                messagebox.showwarning("Calibratron", "Select the local firmware "
                                                      "folder first (or switch to "
                                                      "the GitHub release).")
                return
            if self.fw_report is None:
                self.on_check_folder()
        self._reset_device_views()
        self._spawn(self._run_device_worker, "calibrating…")

    def _reset_device_views(self):
        for var in (*self.par_vars.values(), *self.led_vars.values()):
            var.set("—")
        self.par_tree.delete(*self.par_tree.get_children())
        self.led_tree.delete(*self.led_tree.get_children())

    def _spawn(self, target, status):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Calibratron", "A run is already in progress.")
            return
        _LOG_QUEUE.put(("busy", True))
        _LOG_QUEUE.put(("status", status))

        def wrapped():
            try:
                target()
            except Exception:
                _LOG_QUEUE.put(("log", traceback.format_exc()))
                _LOG_QUEUE.put(("error", "The run failed - see the log for the "
                                         "traceback."))
            finally:
                _LOG_QUEUE.put(("busy", False))
                _LOG_QUEUE.put(("status", "idle"))

        self.worker = threading.Thread(target=wrapped, daemon=True)
        self.worker.start()

    def _flash_local(self, folder, current_version, force):
        """Flash from a checked local folder, mirroring rc.flash_firmware."""
        # Re-checked at flash time, not trusted from the last button press: the
        # entry may have been edited, or the folder contents changed since.
        report = check_firmware_folder(folder)
        if not report["files_ok"]:
            print("[flash] local folder is incomplete - not flashing")
            return 1, None
        version = report["version"]
        if report["verified"]:
            provenance = firmware_fetch.release_provenance(folder)
        else:
            # Flashing is gated on image integrity (just proven above), not on
            # provenance. The record says honestly where the firmware came from.
            print("[flash] local folder has no verified release provenance - "
                  "flashing anyway; the record marks the source unverified")
            provenance = {"source": "local_folder",
                          "path": os.path.abspath(folder),
                          "version": version,
                          "immutable": False, "verified": False,
                          "flash": report["entries"]}
        decision = firmware_fetch.flash_decision(current_version, version,
                                                 force=force, allow_downgrade=False)
        if decision in ("equivalent", "newer", "unknown"):
            print(f"[flash] {decision}: device {current_version!r} vs local "
                  f"{version!r} - skipping flash")
            return 0, provenance
        print(f"[flash] {decision}: {current_version!r} -> {version!r} (local folder)")
        helpers.flash_ambit_firmware(folder, port=self.ports["ambit"])
        time.sleep(1.0)
        helpers.invalidate_port_cache()
        return 0, provenance

    def _run_device_worker(self):
        # 1. Discover (fresh every run: the Ambit was just swapped).
        helpers.invalidate_port_cache()
        ports = helpers.discover_roles(helpers.DEVICE_SPECS)
        self.ports = ports
        _LOG_QUEUE.put(("devices", dict(ports)))

        port_ambit = ports["ambit"]
        if port_ambit is None:
            raise RuntimeError("no Ambit found - is the device seated in the "
                               "flasher fixture?")
        needed = {"par_ref": self.opt_tier3.get(), "dc": self.opt_tier3.get(),
                  "emit_led": self.opt_led.get()}
        missing = [r for r, need in needed.items() if need and not ports.get(r)]
        if missing:
            raise RuntimeError("missing bench instruments for the selected steps: "
                               + ", ".join(missing))

        # 2. As-received state.
        info_asreceived = helpers.ambit_reboot(port_ambit)
        print(info_asreceived)
        fw_asreceived = info_asreceived.firmware
        current_name = info_asreceived.device_name

        # 3. Firmware.
        provenance = None
        if self.opt_flash.get():
            print("\n=== Firmware ===")
            force = self.opt_force.get()
            if force:
                print("WARNING: force - equivalent firmware may be re-flashed "
                      "or an unresponsive device recovered")
            if self.fw_mode.get() == "local":
                rc_code, provenance = self._flash_local(
                    self.fw_folder.get().strip(), fw_asreceived or None, force)
            else:
                rc_code, provenance = rc.flash_firmware(
                    port_ambit, force=force, current_version=fw_asreceived or None,
                    allow_downgrade=rc.ALLOW_FIRMWARE_DOWNGRADE)
            if rc_code != 0:
                print("[flash] continuing with the firmware already on the device")
            else:
                ports = helpers.discover_roles(helpers.DEVICE_SPECS)
                self.ports = ports
                _LOG_QUEUE.put(("devices", dict(ports)))
                port_ambit = ports["ambit"] or port_ambit

        info_before = helpers.ambit_reboot(port_ambit)

        # 4. Name.
        new_name = self.name_var.get().strip() or None
        if new_name and new_name != current_name:
            helpers.set_ambit_name(port_ambit, new_name)
            print(f"[name] {current_name!r} -> {new_name!r}")

        # 5. Reference snapshots.
        port_ref, port_emit = ports.get("par_ref"), ports.get("emit_led")
        port_dc = ports.get("dc")
        reference = helpers.read_minipar_reference(port_ref) if port_ref else None
        reference_emit = (helpers.read_minipar_reference(port_emit)
                          if port_emit else None)

        # 6. Tier-3 PAR.
        tier3_cal = None
        if self.opt_tier3.get():
            print("\n=== Tier-3 PAR calibration (par_slope, par_intercept) ===")
            tier3_cal = rc.calibrate_tier3(port_ambit, port_ref, port_dc,
                                           reference=reference,
                                           upload=self.opt_upload.get())
            _LOG_QUEUE.put(("tier3", tier3_cal))

        # 7. Actinic LED.
        led_cal = None
        if self.opt_led.get():
            print("\n=== Actinic LED calibration ===")
            led_cal = rc.calibrate_led(port_ambit, port_emit,
                                       current_coeff=info_before.act_led_coeff,
                                       upload=self.opt_upload.get())
            _LOG_QUEUE.put(("led", led_cal))

        # 8. Final state, record, publish - same shape as run_calibratron.main().
        print("\n=== Ambit after calibration ===")
        info_after = helpers.ambit_reboot(port_ambit)
        print(info_after)
        try:
            final_cal = helpers.get_spec_cal(port_ambit)
            print(f"Spectral/PAR cal: par_slope={final_cal.par_slope:.6g}, "
                  f"par_intercept={final_cal.par_intercept:.6g}, "
                  f"seed_match={final_cal.seed_match()}")
        except Exception as exc:
            print(f"[readback] spectral/PAR calibration unreadable: {exc}")
            final_cal = None

        print("\n=== Calibration record ===")
        import spec_cal
        import quality
        payload = helpers.make_calibration_payload(
            info_before, info_after,
            spec_par_cal=tier3_cal, led_cal=led_cal, baseline_cal=None,
            protocol_id=rc.OJII_PROTOCOL_ID,
            station={
                "firmware_as_received": fw_asreceived,
                "firmware_release_provenance": provenance,
                "par_reference": reference,
                "led_reference": reference_emit,
                "spec_cal_final": final_cal.to_dict() if final_cal else None,
                "ambit_spec_channels": list(spec_cal.CHANNELS),
                "seed_generation": spec_cal.SEED_GENERATION,
                "adpd_traces": ({"channels": list(quality.ARRUN_CHANNELS),
                                 "pulses": rc.ARRUN_PULSES,
                                 "freq_hz": rc.ARRUN_FREQ_HZ,
                                 "pulse_leds": "zeroing requested before every trace",
                                 "pulse_leds_zeroed_confirmed":
                                     rc._pulse_leds_confirmed(tier3_cal, led_cal),
                                 "stored": "per-point statistics and trace "
                                           "provenance only; raw pulse samples "
                                           "are discarded",
                                 "purpose": "recorded for later analysis; nothing "
                                            "is fitted or written from them"}
                                if rc.RECORD_ADPD_TRACES else None),
                "calibrated_here": ["par_slope", "par_intercept"],
                "shipped_as_firmware_defaults": ["spec_offset", "spec_sens",
                                                 "par_weight"],
                "operator_frontend": "calibratron_gui",
            })
        path = helpers.save_payload(payload, mac=info_after.MAC,
                                    directory=rc.CALIBRATIONS_DIR)

        if self.opt_publish.get():
            topic = (f"experiment/data_ingest/v1/{rc.OJII_EXPERIMENT_ID}/"
                     f"{rc.OJII_SENSOR_FAMILY}/{rc.OJII_SENSOR_VERSION}/"
                     f"{rc.OJII_SENSOR_ID}/{rc.OJII_PROTOCOL_ID}")
            try:
                helpers.publish_payload_mqtt5(payload, topic=topic,
                                              certs_dir=rc.OJII_CERTS_DIR,
                                              endpoint=rc.OJII_ENDPOINT)
                print("[publish] uploaded to openJII")
            except Exception as exc:
                print(f"[publish] openJII upload failed ({exc}); the calibration "
                      f"is saved at {path}")
        else:
            print("[publish] publishing disabled - record saved locally only")

        # 9. Session summary row.
        t3_fit = (tier3_cal or {}).get("fit") or {}
        led_fit = (led_cal or {}).get("fit") or {}
        _LOG_QUEUE.put(("session", {
            "name": new_name or current_name,
            "mac": info_after.MAC,
            "fw": info_after.firmware,
            "slope": _fmt(t3_fit.get("par_slope")),
            "intercept": _fmt(t3_fit.get("par_intercept")),
            "par_r2": _fmt(t3_fit.get("r2"), 6),
            "led_coeff": _fmt(led_fit.get("coefficient")),
            "led_r2": _fmt(led_fit.get("r2"), 6),
            "status": "; ".join(filter(None, (
                (tier3_cal or {}).get("status"),
                "LED written" if (led_cal or {}).get("uploaded") else None))) or "ran",
        }))
        print(f"\n[gui] device done - swap the next Ambit into the fixture, "
              f"enter its name and press Start again")

    # ---- results rendering ---------------------------------------------------

    def _show_tier3(self, record):
        fit = record.get("fit") or {}
        self.par_vars["status"].set(record.get("status") or "—")
        self.par_vars["par_slope"].set(_fmt(fit.get("par_slope")))
        self.par_vars["par_intercept"].set(_fmt(fit.get("par_intercept")))
        self.par_vars["r2"].set(_fmt(fit.get("r2"), 6))
        self.par_vars["nrmse"].set(_fmt(fit.get("nrmse"), 4))
        worst = record.get("confirmation_worst_rel_error")
        self.par_vars["confirm"].set("—" if worst is None else f"{worst:+.2%}")
        self.par_tree.delete(*self.par_tree.get_children())
        for point in record.get("sweep") or []:
            tier2 = ((point.get("ambit") or {}).get("par_tier2"))
            used = "yes" if point.get("usable") else \
                "; ".join(point.get("rejected_because") or ["no"])
            self.par_tree.insert("", "end", text=_fmt(point.get("current_A"), 3),
                                 values=(_fmt(tier2), _fmt(point.get("ref_par")),
                                         used))
        for point in record.get("confirmation") or []:
            rel = point.get("rel_error")
            self.par_tree.insert("", "end", text=_fmt(point.get("current_A"), 3),
                                 values=(_fmt((point.get("ambit") or {}).get("par")),
                                         _fmt(point.get("ref_par")),
                                         "confirm " + ("—" if rel is None
                                                       else f"{rel:+.2%}")))

    def _show_led(self, record):
        fit = record.get("fit") or {}
        status = record.get("status") or ("written and verified"
                                          if record.get("uploaded") else
                                          ("rejected" if fit and not fit.get("passed")
                                           else "preview"))
        self.led_vars["status"].set(status)
        self.led_vars["coefficient"].set(_fmt(fit.get("coefficient")))
        self.led_vars["r2"].set(_fmt(fit.get("r2"), 6))
        self.led_vars["before"].set(_fmt(record.get("act_led_coeff_before")))
        self.led_vars["after"].set(_fmt(record.get("act_led_coeff_after")))
        self.led_tree.delete(*self.led_tree.get_children())
        for setting, ref in zip(record.get("led_settings") or [],
                                record.get("ref_par") or []):
            self.led_tree.insert("", "end", text=str(setting), values=(_fmt(ref, 2),))

    def _add_session_row(self, row):
        self.session_rows += 1
        self.session_tree.insert("", "end", text=str(self.session_rows),
                                 values=(row["name"], row["mac"], row["fw"],
                                         row["slope"], row["intercept"],
                                         row["par_r2"], row["led_coeff"],
                                         row["led_r2"], row["status"]))

    # ---- shutdown -------------------------------------------------------------

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(
                    "Calibratron", "A calibration is still running. Close anyway?\n"
                                   "(The lamp/LED may be left driven.)"):
                return
        cache = rc.FIRMWARE_CACHE_DIR
        # Only ever the runner's own download cache - never a user-selected
        # local firmware folder.
        if os.path.basename(cache) == "firmware_cache" and os.path.isdir(cache):
            shutil.rmtree(cache, ignore_errors=True)
            print(f"[gui] deleted the firmware cache: {cache}")
        self.root.destroy()


def _fmt(value, digits=6):
    """``value`` to ``digits`` significant figures, or an em dash for None."""
    if value is None:
        return "—"
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return str(value)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    CalibratronGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
