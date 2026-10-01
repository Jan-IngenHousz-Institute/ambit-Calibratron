"""Calibratron desktop UI, extracted from PR #4 and using the main calibration backend."""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import traceback
import webbrowser

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk

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
sys.stderr = _Tee(sys.stderr)

import firmware_fetch
import helpers
import openjii_auth
import run_Calibratron as rc
import gui_publish


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

def discover_roles():
    helpers._invalidate_port_cache()
    specs = {"ambit": ("hello\n", "NEW"), "par_ref": ("get_name\n", "Par_REF"),
             "emit_led": ("get_name\n", "Emit_LED"), "dc": ("*IDN?\n", "KIPRIM")}
    return {role: helpers.findDevice(question=q, answer=a, flush=True, timeout=4)
            for role, (q, a) in specs.items()}


def fit_status(record, upload):
    if not record.get("quality", {}).get("passed"):
        return "rejected"
    return "written and verified" if upload else "preview"


ROLES = ("ambit", "par_ref", "emit_led", "dc")
ROLE_LABELS = {
    "ambit": "Ambit (device under calibration)",
    "par_ref": "Par_REF MiniPAR (PAR reference)",
    "emit_led": "Emit_LED MiniPAR (LED reference)",
    "dc": "Kiprim DC source (lamp)",
}


class CalibratronGUI:
    def __init__(self, root: tk.Tk, *, auto_sign_in=True):
        self.root = root
        root.title("Calibratron")
        root.geometry("1180x800")
        root.minsize(980, 640)

        self.ports = {role: None for role in ROLES}
        self.worker = None
        self.fw_report = None
        self.session_rows = 0
        self.settings = openjii_auth.Settings.load()
        #: Validated OpenJIIClient, or None while nobody is signed in.
        self.oj_client = None
        self._auth_generation = 0

        self._build_statusbar()
        self._build_left()
        self._build_right()

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._drain_queue)
        self._log_line("[gui] ready - press 'Rescan bench' to discover the instruments")
        if auto_sign_in:
            self._sign_in_with_stored_key()

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
                                    height=4)
        self.fw_tree.heading("#0", text="file")
        self.fw_tree.heading("ok", text="ok")
        self.fw_tree.heading("detail", text="detail")
        self.fw_tree.column("#0", width=130, anchor="w")
        self.fw_tree.column("ok", width=30, anchor="center")
        self.fw_tree.column("detail", width=190, anchor="w")
        self.fw_tree.grid(row=4, column=0, columnspan=3, sticky="ew")
        self._fw_mode_changed()

        # openJII
        oj = ttk.LabelFrame(left, text="openJII", padding=6)
        oj.pack(fill="x", pady=(8, 0))
        oj.columnconfigure(1, weight=1)
        ttk.Label(oj, text="Environment:").grid(row=0, column=0, sticky="w")
        self.oj_env = tk.StringVar(value=self.settings.environment)
        env_box = ttk.Combobox(oj, textvariable=self.oj_env, width=8,
                               state="readonly",
                               values=list(openjii_auth.ENVIRONMENTS))
        env_box.grid(row=0, column=1, sticky="w")
        env_box.bind("<<ComboboxSelected>>", lambda _e: self._on_env_changed())
        self.oj_sign_in_btn = ttk.Button(oj, text="Sign in (API key)…",
                                         command=self.on_sign_in)
        self.oj_sign_in_btn.grid(row=1, column=0, columnspan=2, sticky="ew",
                                 pady=(4, 0))
        self.oj_user = tk.StringVar(value="not signed in")
        ttk.Label(oj, textvariable=self.oj_user, foreground="gray",
                  wraplength=240, justify="left").grid(
            row=2, column=0, columnspan=2, sticky="w")

        # Run
        run = ttk.LabelFrame(left, text="Calibrate this device", padding=6)
        run.pack(fill="x", pady=(8, 0))
        self.opt_flash = tk.BooleanVar(value=True)
        self.opt_force = tk.BooleanVar(value=rc.FORCE_FLASH_FIRMWARE)
        self.opt_par = tk.BooleanVar(value=True)
        self.opt_led = tk.BooleanVar(value=True)
        self.opt_upload = tk.BooleanVar(value=rc.UPLOAD_GAINS)
        self.opt_publish = tk.BooleanVar(value=False)
        for i, (text, var) in enumerate((
                ("Flash firmware", self.opt_flash),
                ("Force reflash of equivalent firmware", self.opt_force),
                ("PAR sensor calibration (light_slope)", self.opt_par),
                ("Actinic LED calibration", self.opt_led),
                ("Write coefficients to the device", self.opt_upload),
                ("Publish the record to openJII", self.opt_publish)), start=0):
            ttk.Checkbutton(run, text=text, variable=var).grid(
                row=i, column=0, columnspan=2, sticky="w")
        self.start_btn = ttk.Button(run, text="Start calibration",
                                    command=self.on_start)
        self.start_btn.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Label(run, text="The device name is asked for once the Ambit has "
                            "been read, at the start of the run.",
                  wraplength=240, justify="left").grid(
            row=7, column=0, columnspan=2, sticky="w", pady=(4, 0))

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

        par = ttk.LabelFrame(cur, text="PAR fit (reference PAR = gain × raw PAR)",
                             padding=6)
        par.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, 6))
        par.columnconfigure(1, weight=1)
        par.rowconfigure(6, weight=1)
        self.par_vars = {}
        for i, (key, label) in enumerate((
                ("status", "Status"), ("par_slope", "PAR gain"),
                ("par_intercept", "Fit intercept (fixed)"), ("r2", "R²"),
                ("nrmse", "NRMSE"), ("confirm", "QC reasons"))):
            ttk.Label(par, text=label + ":").grid(row=i, column=0, sticky="w")
            var = tk.StringVar(value="—")
            ttk.Label(par, textvariable=var).grid(row=i, column=1, sticky="w")
            self.par_vars[key] = var
        self.par_tree = ttk.Treeview(
            par, columns=("tier2", "ref", "used"), show="tree headings", height=9)
        self.par_tree.heading("#0", text="lamp A")
        self.par_tree.heading("tier2", text="raw PAR")
        self.par_tree.heading("ref", text="ref PAR")
        self.par_tree.heading("used", text="fitted")
        self.par_tree.column("#0", width=70, anchor="e")
        self.par_tree.column("tier2", width=90, anchor="e")
        self.par_tree.column("ref", width=90, anchor="e")
        self.par_tree.column("used", width=110, anchor="w")
        self.par_tree.grid(row=6, column=0, columnspan=2, sticky="nsew", pady=(6, 0))

        led = ttk.LabelFrame(cur, text="Actinic LED fit (setting = gain × ref PAR)",
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
        heads = ("name", "MAC", "firmware", "PAR gain", "Fit intercept",
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
                elif kind == "par":
                    self._show_par(payload)
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
                elif kind == "ask_name":
                    self._prompt_device_name(payload)
                elif kind == "signed_in":
                    self._show_signed_in(payload)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_queue)

    # ---- openJII sign-in ----------------------------------------------------

    def _env(self):
        return openjii_auth.environment(self.oj_env.get())

    def _on_env_changed(self):
        self._auth_generation += 1
        self.settings.environment = self.oj_env.get()
        self.settings.save()
        self.oj_client = None
        self.oj_user.set("not signed in")
        self._sign_in_with_stored_key()

    def _sign_in_with_stored_key(self):
        """Reuse a key already on this PC - this tool's, or the flash GUI's.

        Same operator, same openJII account, so a bench that has signed in
        once never sees the dialog again.
        """
        env = self._env()
        key = self.settings.api_key(env.key)
        source = "stored key"
        if not key:
            key = openjii_auth.flash_gui_api_key(env.key)
            source = "the ambyte flash GUI's key"
        if not key:
            self._log_line(f"[openJII] not signed in to {env.key} - press "
                           f"'Sign in (API key)…' before publishing")
            return
        self._log_line(f"[openJII] validating {source} for {env.key}…")
        self._validate_key_async(key, quiet=True)

    def on_sign_in(self):
        env = self._env()
        webbrowser.open(env.api_keys_url)
        key = simpledialog.askstring(
            "openJII sign-in",
            f"A browser window opened at:\n{env.api_keys_url}\n\n"
            f"Sign in there, create a personal API key (it is shown once),\n"
            f"and paste it here (jii_...):",
            initialvalue=(self.settings.api_key(env.key)
                          or openjii_auth.flash_gui_api_key(env.key)),
            parent=self.root)
        if key and key.strip():
            self._validate_key_async(key.strip())

    def _validate_key_async(self, key, quiet=False):
        """Validate off the main thread: this is a network round trip, and it
        happens while the operator is mounting the next device."""
        env = self._env()
        self._auth_generation += 1
        generation = self._auth_generation
        self.oj_client = None
        self.oj_user.set("validating…")

        def work():
            client = openjii_auth.OpenJIIClient(env, key)
            try:
                client.validate_key()
            except openjii_auth.OpenJIIError as exc:
                if quiet:
                    _LOG_QUEUE.put(("log", f"[openJII] {exc}\n"))
                else:
                    _LOG_QUEUE.put(("error", f"openJII sign-in: {exc}"))
                return
            _LOG_QUEUE.put(("signed_in", (generation, client, key)))

        threading.Thread(target=work, daemon=True).start()

    def _show_signed_in(self, payload):
        generation, client, key = payload
        if generation != self._auth_generation or client.env.key != self.oj_env.get():
            return
        self.oj_client = client
        self.settings.set_api_key(client.env.key, key)
        self.settings.environment = client.env.key
        self.settings.save()
        self.oj_user.set(f"{client.env.key}: {client.who()}")
        self._log_line(f"[openJII] signed in to {client.env.key} as "
                       f"{client.who()}")

    def _ask_device_name(self, current_name):
        """Ask the operator what to call this Ambit; return a name or None.

        Called from the worker thread: the request is queued and the worker
        blocks until the dialog closes on the main thread. Blank input and
        Cancel both mean "keep the current name" - no device is renamed by
        accident, and a run never stalls on a dismissed dialog.
        """
        request = {"current": current_name, "answer": None,
                   "done": threading.Event()}
        _LOG_QUEUE.put(("ask_name", request))
        request["done"].wait()
        return request["answer"]

    def _prompt_device_name(self, request):
        """Main-thread half of :meth:`_ask_device_name`."""
        current = request["current"] or "(no name set)"
        try:
            answer = simpledialog.askstring(
                "Calibratron - device name",
                f"Current name: {current}\n\n"
                f"New name for this Ambit\n"
                f"(leave blank to keep the current one):",
                parent=self.root)
            request["answer"] = (answer or "").strip() or None
        except Exception:
            _LOG_QUEUE.put(("log", traceback.format_exc()))
            request["answer"] = None
        finally:
            request["done"].set()

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for btn in (self.start_btn, self.rescan_btn, self.fw_check_btn,
                    self.fw_browse_btn):
            btn.configure(state=state)

    # ---- device discovery --------------------------------------------------

    def on_rescan(self):
        self._spawn(self._rescan_worker, "scanning the bench…")

    def _rescan_worker(self):
        helpers._invalidate_port_cache()
        ports = discover_roles()
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
                                f"no verified release provenance - flashing blocked")
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
        if not self._publish_preflight():
            return
        self._reset_device_views()
        self.run_options = {
            "par": self.opt_par.get(), "led": self.opt_led.get(),
            "flash": self.opt_flash.get(), "force": self.opt_force.get(),
            "upload": self.opt_upload.get(), "publish": self.opt_publish.get(),
            "mode": self.fw_mode.get(), "folder": self.fw_folder.get().strip(),
            "client": self.oj_client,
        }
        self._spawn(self._run_device_worker, "calibrating…")

    def _publish_preflight(self):
        """Settle publishing before the run, not after it.

        An unauthenticated upload used to surface as a warning at the end of a
        twenty-minute calibration, with the operator already unplugging the
        device. :return: True to go ahead.
        """
        if not self.opt_publish.get() or self.oj_client is not None:
            return True
        if messagebox.askyesno(
                "Calibratron",
                "Publishing to openJII is on, but nobody is signed in.\n\n"
                "Sign in now? (No = run this device without publishing; "
                "the record is still saved to disk.)"):
            self.on_sign_in()
            # Validation is a network round trip on its own thread, so the
            # client is not up yet: let the operator press Start again rather
            # than blocking the window on it.
            return False
        self.opt_publish.set(False)
        self._log_line("[openJII] publishing turned off for this run - "
                       "not signed in")
        return True

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
        if not report["verified"]:
            raise RuntimeError("Local firmware must be a complete verified immutable release cache entry")
        version = report["version"]
        provenance = firmware_fetch.release_provenance(folder)
        decision = firmware_fetch.flash_decision(current_version, version,
                                                 force=force, allow_downgrade=False)
        if decision in ("equivalent", "newer", "unknown"):
            print(f"[flash] {decision}: device {current_version!r} vs local "
                  f"{version!r} - skipping flash")
            return 0, provenance
        print(f"[flash] {decision}: {current_version!r} -> {version!r} (local folder)")
        helpers.flash_ambit_firmware(folder)
        time.sleep(1.0)
        helpers._invalidate_port_cache()
        return 0, provenance

    def _run_device_worker(self):
        options = self.run_options
        ports = discover_roles()
        self.ports = ports
        _LOG_QUEUE.put(("devices", dict(ports)))
        required = {"ambit": True, "par_ref": options["par"],
                    "dc": options["par"], "emit_led": options["led"]}
        missing = [role for role, needed in required.items() if needed and not ports[role]]
        if missing:
            raise RuntimeError("Missing bench instruments: " + ", ".join(missing))
        port = ports["ambit"]
        before = helpers.ambit_reboot(port)
        current_name = before.name.decode(errors="replace").strip()
        current_fw = before.FW.decode(errors="replace").strip()
        new_name = self._ask_device_name(current_name)
        provenance = None
        if options["flash"]:
            if options["mode"] == "local":
                _, provenance = self._flash_local(options["folder"], current_fw, options["force"])
            else:
                rc._firmware_release_provenance = None
                result = rc.flash_firmware(force_flash=options["force"], current_version=current_fw)
                if result:
                    raise RuntimeError("Could not obtain or verify firmware; calibration was not started")
                provenance = rc._firmware_release_provenance
            ports = discover_roles()
            self.ports = ports
            _LOG_QUEUE.put(("devices", dict(ports)))
            port = ports["ambit"]
            if not port:
                raise RuntimeError("Ambit did not return after firmware check")
            before = helpers.ambit_reboot(port)
        if new_name and new_name != current_name:
            helpers.set_ambit_name(port, new_name)
        par_cal = led_cal = None
        if options["par"]:
            par_cal = rc.calibrate_par_sensor(port, ports["par_ref"], ports["dc"],
                                              upload=options["upload"], show_plot=False)
            _LOG_QUEUE.put(("par", (par_cal, options["upload"])))
        if options["led"]:
            led_cal = rc.calibrate_led(port, ports["emit_led"],
                                      upload=options["upload"], show_plot=False)
        after = helpers.ambit_reboot(port)
        if led_cal is not None:
            display = dict(led_cal, act_led_coeff_before=before.act_led_coeff,
                           act_led_coeff_after=after.act_led_coeff)
            _LOG_QUEUE.put(("led", (display, options["upload"])))
        payload = helpers.make_calibration_payload(
            before, after, par_cal=par_cal, led_cal=led_cal,
            firmware_release_provenance=provenance)
        path = rc.save_payload(payload, mac=after.MAC)
        if options["publish"]:
            try:
                client = options["client"]
                gui_publish.publish_payload_mqtt5_wss(
                    payload,
                    topic="experiment/data_ingest/v1/993ae58e-2e87-45ef-96e1-5bbdb0916817/ambit/v1.0/ambit_calibration_1/1234556",
                    endpoint=client.env.mqtt_host, credentials=client.iot_credentials(),
                    client_id="ambit_calibration_1")
            except Exception as exc:
                print(f"[publish] upload failed ({exc}); record saved at {path}")
        _LOG_QUEUE.put(("session", {
            "name": new_name or current_name, "mac": after.MAC,
            "fw": after.FW.decode(errors="replace").strip(),
            "slope": _fmt((par_cal or {}).get("slope")), "intercept": "0",
            "par_r2": _fmt((par_cal or {}).get("r2")),
            "led_coeff": _fmt((led_cal or {}).get("slope")),
            "led_r2": _fmt((led_cal or {}).get("r2")),
            "status": "; ".join(f"{name}: {fit_status(cal, options['upload'])}"
                                for name, cal in (("PAR", par_cal), ("LED", led_cal))
                                if cal is not None) or "saved",
        }))
        print(f"[gui] device done; record saved at {path}")

    def _show_par(self, result):
        record, upload = result
        quality = record.get("quality") or {}
        self.par_vars["status"].set(fit_status(record, upload))
        self.par_vars["par_slope"].set(_fmt(record.get("slope")))
        self.par_vars["par_intercept"].set("0")
        self.par_vars["r2"].set(_fmt(record.get("r2")))
        self.par_vars["nrmse"].set(_fmt(quality.get("nrmse")))
        self.par_vars["confirm"].set("; ".join(quality.get("reasons", [])) or "passed")
        self.par_tree.delete(*self.par_tree.get_children())
        for current, raw, ref in zip(record["currents_A"], record["x"], record["y"]):
            self.par_tree.insert("", "end", text=_fmt(current),
                                 values=(_fmt(raw), _fmt(ref), "yes"))

    def _show_led(self, result):
        record, upload = result
        self.led_vars["status"].set(fit_status(record, upload))
        self.led_vars["coefficient"].set(_fmt(record.get("slope")))
        self.led_vars["r2"].set(_fmt(record.get("r2")))
        self.led_vars["before"].set(_fmt(record.get("act_led_coeff_before")))
        self.led_vars["after"].set(_fmt(record.get("act_led_coeff_after")))
        self.led_tree.delete(*self.led_tree.get_children())
        for ref, setting in zip(record["x"], record["y"]):
            self.led_tree.insert("", "end", text=_fmt(setting), values=(_fmt(ref),))

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
