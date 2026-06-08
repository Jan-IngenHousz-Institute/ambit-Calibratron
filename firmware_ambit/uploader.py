"""Standalone Ambit firmware uploader.

A root-level sibling of ``ambyte_uploader.py``. The script searches its own
folder and every sub-folder for the compiled Ambit firmware images (and the
bundled ``esptool.exe``), so it just needs to be dropped next to -- or one
level above -- the compiled files; it does not need its own copy of them.

Cross-platform esptool resolution, forced-or-detected flashing, and an
optional post-flash hardware test.
"""

import os
import subprocess
import sys
import time
import argparse
import re
import importlib
import importlib.util


# ===========================================================================
# CONFIGURATION -- edit these to control how the uploader behaves
# ===========================================================================

# Flashing / test behaviour.
FORCE_FLASH = True   # True: always flash. False: flash only on "invalid header".
RUN_TEST = False      # True: run the post-flash hardware test (test_ambit()).

# Max time to wait for pip when auto-installing a missing dependency.
PIP_INSTALL_TIMEOUT = 300  # seconds

# MLX90632 temperature-sensor acceptance limits.
MIN_TEMP = -10             # deg C, lower bound for a valid reading
MAX_TEMP = 40              # deg C, upper bound for a valid reading
MLX_READ_TIME_LIMIT = 100  # ms, max acceptable sensor read time

# WCH CH343 USB-serial bridge on the Ambit flasher.
TARGET_VID = 0x1A86
TARGET_PID = 0x55D4
TARGET_VIDPID = "1A86:55D4"

# Firmware images the script searches for. Whichever folder (this one or a
# sub-folder) contains all of them is used as the firmware folder. esptool is
# resolved separately by esptool_command(), so it is *not* listed here.
REQUIRED_FILES = [
    "ambit-1.ino.bin",
    "ambit-1.ino.bootloader.bin",
    "ambit-1.ino.partitions.bin",
    "boot_app0.bin",
]
# ===========================================================================


def ensure_package(module_name, pip_name=None):
    """Import ``module_name``; if it is missing, install it with pip first.

    This lets the uploader run on a fresh machine without a separate
    ``pip install`` step -- the dependency is fetched automatically the first
    time the script is run.

    :param module_name: the name used in ``import`` (e.g. ``serial``).
    :param pip_name: the package name on PyPI, when it differs from
        ``module_name`` (e.g. ``pyserial``).
    :return: the imported module.
    """
    pip_name = pip_name or module_name
    try:
        return importlib.import_module(module_name)
    except ImportError:
        pass

    print(f"[INFO] Required package '{pip_name}' is not installed; "
          f"installing it now (one-time setup)...")
    for extra in ([], ["--user"]):
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", *extra, pip_name],
                check=True,
                timeout=PIP_INSTALL_TIMEOUT,
            )
            break
        except subprocess.CalledProcessError:
            continue
        except subprocess.TimeoutExpired:
            print(f"[WARN] pip install of '{pip_name}' timed out after "
                  f"{PIP_INSTALL_TIMEOUT}s.")
            continue

    # A freshly installed package may not be on the import path yet.
    importlib.invalidate_caches()
    try:
        import site
        for path in (*site.getsitepackages(), site.getusersitepackages()):
            if path not in sys.path:
                sys.path.append(path)
    except Exception:
        pass

    try:
        return importlib.import_module(module_name)
    except ImportError:
        print(f"[ERROR] Could not install '{pip_name}' automatically. Please "
              f"install it manually with:\n"
              f"        {sys.executable} -m pip install {pip_name}")
        sys.exit(1)


# Make sure third-party dependencies are present before importing them.
ensure_package("serial", "pyserial")
if os.name != "nt":
    # Windows uses the bundled esptool.exe; other platforms need the package.
    ensure_package("esptool")

import serial
import serial.tools.list_ports

# Folder this script lives in. The firmware files (and esptool.exe) are looked
# up here and in every sub-folder, so the script can sit next to -- or one
# level above -- the compiled firmware.
HERE = os.path.dirname(os.path.abspath(__file__))

# Folder that actually holds the compiled firmware. Discovered at runtime by
# main(); esptool_command() prefers an esptool.exe found inside it.
FIRMWARE_DIR = None


def find_file(start_dir, filename):
    """Return the path to the first ``filename`` found in ``start_dir`` or any
    of its sub-folders, or ``None`` if it is nowhere to be found.
    """
    for root, _dirs, files in os.walk(start_dir):
        if filename in files:
            return os.path.join(root, filename)
    return None


def find_firmware_dir(start_dir, required_files):
    """Search ``start_dir`` and its sub-folders for a folder that contains
    every file in ``required_files``.

    :return: the absolute path of the first matching folder, or ``None``.
    """
    required = set(required_files)
    for root, _dirs, files in os.walk(start_dir):
        if required.issubset(files):
            return os.path.abspath(root)
    return None


def esptool_command():
    """Return the argv prefix used to invoke esptool, cross-platform.

    Prefers a bundled ``esptool.exe`` on Windows -- looked for inside the
    firmware folder first, then this script's folder and every sub-folder --
    otherwise runs the installed ``esptool`` Python package via
    ``python -m esptool`` (Linux/macOS, or Windows without the bundled binary).

    :raises RuntimeError: if no esptool is available.
    """
    if os.name == "nt":
        local_exe = None
        if FIRMWARE_DIR:
            local_exe = find_file(FIRMWARE_DIR, "esptool.exe")
        if not local_exe:
            local_exe = find_file(HERE, "esptool.exe")
        if local_exe:
            return [local_exe]
    if importlib.util.find_spec("esptool") is not None:
        return [sys.executable, "-m", "esptool"]
    raise RuntimeError(
        "esptool not found. Install it with `pip install esptool`, "
        "or place esptool.exe next to this script or in a sub-folder "
        "(Windows only)."
    )


def serial_ports():
    flasher_ports = []
    for port in sorted(serial.tools.list_ports.comports()):
        try:
            device = getattr(port, "device", None)
            if not device:
                continue
            hwid = (getattr(port, "hwid", "") or "").upper()
            matches_vidpid = (getattr(port, "vid", None), getattr(port, "pid", None)) == (TARGET_VID, TARGET_PID)
            if matches_vidpid or TARGET_VIDPID in hwid:
                flasher_ports.append(device)
        except Exception:
            continue
    return flasher_ports


def detect_invalid_header(port, timeout_s=5, baud=115200):
    """
    Listen briefly on the serial port and detect the "invalid header" string.
    """
    serial_output = ""
    print(f"[INFO] Listening on {port} for {timeout_s}s to detect boot status...")

    try:
        with serial.Serial(port, baud, timeout=0.1) as ser:
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                chunk = ser.read(128)
                if not chunk:
                    continue
                serial_output += chunk.decode(errors="replace")
                if "invalid header" in serial_output:
                    return True, serial_output
    except serial.SerialException as exc:
        print(f"[ERROR] Could not open serial port {port}: {exc}")

    return False, serial_output


def flash(port):
    cmd = [
        *esptool_command(),
        "--chip",
        "esp32c3",
        "--baud",
        "921600",
        "--port",
        port,
        "--before",
        "default_reset",
        "--after",
        "hard_reset",
        "write_flash",
        "-z",
        "--flash_mode",
        "keep",
        "--flash_freq",
        "keep",
        "--flash_size",
        "keep",
        "0x0",
        "ambit-1.ino.bootloader.bin",
        "0x8000",
        "ambit-1.ino.partitions.bin",
        "0xe000",
        "boot_app0.bin",
        "0x10000",
        "ambit-1.ino.bin",
    ]

    print(f"[INFO] Flashing {port} with esptool...")
    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"esptool exited with return code {result.returncode}")
    print("[INFO] Flash completed.")


def parse_bool_arg(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return False

    val = str(value).strip().lower()
    if val in {"true", "1", "yes", "y", "on"}:
        return True
    if val in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError("Boolean argument must be true or false.")


def ambit_readlines(ser, timeout=1.0, invalid_bahave=False, max_lines=1, ending_line = ""):
    lines = []
    line = ""
    t0 = time.perf_counter()
    while (time.perf_counter() - t0) < timeout:
        if ser.in_waiting > 0:
            r = ser.read()
            if r < bytes([128]):
                line += r.decode(errors="replace")
            else:
                if invalid_bahave:
                    break
            if r == b"\n":
                lines.append(line)
                line = ""
                if ending_line and ending_line in lines[-1]:
                    break
        else:
            time.sleep(0.1)
        if len(lines) >= max_lines:
            break
    return lines


def is_increasing(values):
    """True when each value in a sweep is larger than the one before it.

    The gain / current sweeps step the photodiode amplification up, so a
    healthy channel returns readings that climb monotonically.
    """
    return len(values) > 1 and all(b > a for a, b in zip(values, values[1:]))


def test_ambit(port: str):

    ret_dict: dict[str, bool] = {"FW": False, "ADPD": False, "AS7341": False, "MLX90632": False, "Temp": False, "LightPass-SunPD": False, "LightPass-LeafPD": False, "LightPass-SignalPD": False, "LightPass-RefPD": False}

    ambit_ready = 0
    with serial.Serial(port, 115200) as ser:
        trials = 50
        print(f"Trying to detect Ambit {trials} times")
        ser.write(b"\r\n")
        ser.flush()
        time.sleep(0.1)

        for _ in range(trials):
            # Drop any stale/buffered output (e.g. the boot log) before each
            # attempt, so we read the fresh reply to *this* hello instead of
            # chewing through a backlog one line at a time over all 50 trials.
            ser.reset_input_buffer()
            ser.write(b"hello\r\n")
            lines = ambit_readlines(ser, timeout=0.5, invalid_bahave=True, max_lines=1)
            print(f"Reading: {lines}; {_+1}/{trials} waiting for 'NEW Name Here Ready'...")
            if len(lines) == 0:
                continue

            if "NEW Name Here Ready" in lines[0]:
                print("[PASS]\t\tAmbit is detected")
                ret_dict["FW"] = True
                ambit_ready = 1
                break

            if ambit_ready == 0:
                print("Received: {}".format(lines[0]), end="")
            # time.sleep(1)

        if ambit_ready == 0:
            print("[FAILED]\tAmbit detection failed")
            return ret_dict

        ser.write(b"check\r\n")
        lines = ambit_readlines(ser, timeout=5, invalid_bahave=False, max_lines=50, ending_line="Done!!")

        adpd_match = re.compile(r"Checking ADPD\s+ADPD Found, chip version: (\d+)")
        as7341_match = re.compile(r"Checking AS7341\s+Success\s+(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
        mlx_match = re.compile(r"Checking MLX90632\s+Success\s+(\d+)\s+([\d.]+)\s+([\d.]+)")
        chip_match = re.compile(r"ESP32Temp\s+([\d.]+)")
        sunPD_match = re.compile(r"Sun PD\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")
        leafPD_match = re.compile(r"Leaf PD\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")
        signal_match = re.compile(r"Signal\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")
        ref_match = re.compile(r"Ref\t\t(\d+)\t(\d+)\t(\d+)\t(\d+)\t(\d+)\n")

        light_intensity = 0
        chip_temp = -100.0
        temp1, temp2 = 100.0, 200.0

        for line in lines:
            if adpd_match.match(line):
                ret_dict["ADPD"] = True
                continue

            if chip_match.match(line):
                ret = chip_match.findall(line)
                if ret[0][0].isnumeric():
                    chip_temp = float(ret[0])
                continue

            if as7341_match.match(line):
                ret = as7341_match.findall(line)
                for n in ret[0]:
                    if n.isnumeric():
                        light_intensity += int(n)
                if light_intensity > 5:
                    ret_dict["AS7341"] = True
                    print("[PASS]\t\tAS7341 Found, light intensity: {}".format(light_intensity))
                else:
                    print("[FAILED]\tAS7341 Found, but light intensity too low: {}".format(light_intensity))
                continue

            if mlx_match.match(line):
                ret = mlx_match.findall(line)
                read_time = int(ret[0][0])
                temp1 = float(ret[0][1])
                temp2 = float(ret[0][2])
                if read_time < MLX_READ_TIME_LIMIT and temp1 > MIN_TEMP and temp1 < MAX_TEMP and temp2 > MIN_TEMP and temp2 < MAX_TEMP:
                    ret_dict["MLX90632"] = True
                    print("[PASS]\t\tMLX90632 Found, reading time:{}, die temp: {}, object temp: {}".format(read_time, temp1, temp2))
                else:
                    if read_time >= MLX_READ_TIME_LIMIT:
                        print("[FAILED]\tMLX90632 read time too long: {}".format(read_time))
                    else:
                        print("[FAILED]\tMLX90632 Found, reading time:{}, die temp: {}, object temp: {}".format(ret[0][0], ret[0][1], ret[0][2]))
                continue

            if sunPD_match.match(line):
                arr = [int(n) for n in sunPD_match.findall(line)[0]]
                print("Sun PD values:", arr)
                if is_increasing(arr):
                    print("[PASS]\t\t<SUN> PD gain sweep")
                    ret_dict["LightPass-SunPD"] = True
                else:
                    print("[FAILED]\t<SUN> PD gain sweep not increasing!")
                continue

            if leafPD_match.match(line):
                arr = [int(n) for n in leafPD_match.findall(line)[0]]
                print("Leaf PD values:", arr)
                if is_increasing(arr):
                    print("[PASS]\t\t<Leaf> PD gain sweep")
                    ret_dict["LightPass-LeafPD"] = True
                else:
                    print("[FAILED]\t<Leaf> PD gain sweep not increasing!")
                continue

            if signal_match.match(line):
                arr = [int(n) for n in signal_match.findall(line)[0]]
                print("Signal PD values:", arr)
                if is_increasing(arr):
                    print("[PASS]\t\t<Signal> PD Current sweep")
                    ret_dict["LightPass-SignalPD"] = True
                else:
                    print("[FAILED]\t<Signal> PD Current sweep not increasing!")
                continue

            if ref_match.match(line):
                arr = [int(n) for n in ref_match.findall(line)[0]]
                print("Ref PD values:", arr)
                if is_increasing(arr):
                    print("[PASS]\t\t<Ref> PD Current sweep")
                    ret_dict["LightPass-RefPD"] = True
                else:
                    print("[FAILED]\t<Ref> PD Current sweep not increasing!")
                continue
                

    if abs(chip_temp * 2 - temp1 - temp2) > 30:
        if ret_dict["MLX90632"]:
            print("[FAILED]\tTemperature reading mismatch, chip temp: {}, mlx temp: {}, {}".format(chip_temp, temp1, temp2))
    else:
        ret_dict["Temp"] = True

    return ret_dict


def run_post_flash_tests(port):
    """Run test_ambit(), retrying up to 5 times if any check fails."""
    print("[INFO] Starting post-flash tests")
    results = test_ambit(port)
    if not all(results.values()):
        print("[WARN] Some tests failed.")
        print("[WARN] Test details: {}".format(results))
        print("[INFO] Trying again 5 times with 1s delay...")
        for attempt in range(5):
            time.sleep(1)
            results = test_ambit(port)
            if all(results.values()):
                print("[INFO] All tests passed on attempt {}".format(attempt + 1))
                break
            else:
                print("[WARN] Attempt {} failed: {}".format(attempt + 1, results))
    print("[INFO] Test result: {}".format(results))
    return results


def main(force_flash=True, run_test=False):
    # Find the folder that holds the compiled firmware -- this script's own
    # folder or any sub-folder -- and work from there so the .bin files
    # resolve by bare name.
    global FIRMWARE_DIR
    firmware_dir = find_firmware_dir(HERE, REQUIRED_FILES)
    if firmware_dir is None:
        print(f"[ERROR] Could not find the firmware files "
              f"({', '.join(REQUIRED_FILES)}) in {HERE} or any sub-folder.")
        return 1
    FIRMWARE_DIR = firmware_dir
    os.chdir(firmware_dir)
    print(f"[INFO] Using firmware folder: {firmware_dir}")

    try:
        print(f"[INFO] Using esptool: {' '.join(esptool_command())}")
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        return 1

    ports = serial_ports()
    if not ports:
        print("[ERROR] No matching Ambit flasher USB device found.")
        return 1
    if len(ports) != 1:
        print(f"[ERROR] Expected 1 flasher port, found {len(ports)}: {', '.join(ports)}")
        return 1

    port = ports[0]
    print(f"[INFO] Using port: {port}")

    # ---- Flash: forced, or only when an "invalid header" is detected -------
    flashed = False
    if force_flash:
        print("[INFO] force_flash=true, flashing.")
        flash(port)
        flashed = True
    else:
        needs_flash, serial_log = detect_invalid_header(port)
        if needs_flash:
            print("\n[WARN] Invalid header detected. Flashing required.")
            flash(port)
            flashed = True
        elif serial_log.strip():
            print("\n[INFO] No 'invalid header' detected; flashing is not required.")
        else:
            print("\n[WARN] No serial output detected during probe; no flash attempt done.")

    # ---- Post-flash hardware test -----------------------------------------
    if run_test:
        if flashed:
            print("[INFO] Waiting 1 second before running test...")
            time.sleep(1)
        run_post_flash_tests(port)
    else:
        print("[INFO] run_test=false, skipping test_ambit()")

    return 0


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser(
            description="Flash Ambit firmware using detection or forced mode."
        )

        parser.add_argument(
            "--force-flash",
            nargs="?",
            default=f"{FORCE_FLASH}",
            const="true",
            help="Pass --force-flash=true/false or --force-flash (for true). "
                 "When false, the firmware is only flashed if an 'invalid header' "
                 "is detected on boot.",
        )

        parser.add_argument(
            "--run-test",
            nargs="?",
            default=f"{RUN_TEST}",
            const="true",
            help="Pass --run-test or --run-test=true to run test_ambit() after flashing.",
        )
        args = parser.parse_args()

        force_flash = parse_bool_arg(args.force_flash)
        run_test = parse_bool_arg(args.run_test)
        sys.exit(main(force_flash=force_flash, run_test=run_test))
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.")
        sys.exit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)
