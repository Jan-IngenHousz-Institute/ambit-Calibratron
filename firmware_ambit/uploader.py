import os
import serial
import serial.tools.list_ports
import subprocess
import sys
import time
import argparse
import re
import importlib.util

MIN_TEMP = -10
MAX_TEMP = 40
MLX_READ_TIME_LIMIT = 100


# Firmware images that must sit next to this script (the flasher tool itself is
# resolved separately by esptool_command(), so it is *not* listed here).
REQUIRED_FILES = [
    "ambit-1.ino.bin",
    "ambit-1.ino.bootloader.bin",
    "ambit-1.ino.partitions.bin",
    "boot_app0.bin",
]

# WCH CH343 USB-serial bridge on the Ambit flasher.
TARGET_VID = 0x1A86
TARGET_PID = 0x55D4
TARGET_VIDPID = "1A86:55D4"


def esptool_command():
    """Return the argv prefix used to invoke esptool, cross-platform.

    Prefers the bundled ``esptool.exe`` on Windows when present; otherwise runs
    the installed ``esptool`` Python package via ``python -m esptool`` (Linux/
    macOS, or Windows without the bundled binary).

    :raises RuntimeError: if no esptool is available.
    """
    local_exe = os.path.join(os.path.dirname(os.path.abspath(__file__)), "esptool.exe")
    if os.name == "nt" and os.path.isfile(local_exe):
        return [local_exe]
    if importlib.util.find_spec("esptool") is not None:
        return [sys.executable, "-m", "esptool"]
    raise RuntimeError(
        "esptool not found. Install it with `pip install esptool`, "
        "or place esptool.exe next to this script (Windows only)."
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


def ambit_readlines(ser: serial.Serial, timeout: float = 1.0, invalid_bahave: bool = False, max_lines: int = 1) -> list[str]:
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
        else:
            time.sleep(0.1)
        if len(lines) >= max_lines:
            break
    return lines


def test_ambit(port: str):
    ret_dict = {"FW": False, "ADPD": False, "AS7341": False, "MLX90632": False, "Temp": False, "LightPass": False}

    ambit_ready = 0
    with serial.Serial(port, 115200) as ser:
        trials = 50
        print(f"Trying to detect Ambit {trials} times")

        for _ in range(trials):
            ser.write(b"hello\r\n")
            lines = ambit_readlines(ser, timeout=2.0, invalid_bahave=True, max_lines=1)
            print(f"Reading: {lines}; {_+1}/{trials} waiting for 'NEW Name Here Ready'...")
            if len(lines) == 0:
                continue

            if "NEW Name Here Ready" in lines[0]:
                print("Ambit is detected")
                ret_dict["FW"] = True
                ambit_ready = 1
                break

            if ambit_ready == 0:
                print("Received: {}".format(lines[0]), end="")
            time.sleep(1)

        if ambit_ready == 0:
            print("Ambit detection failed")
            return ret_dict

        ser.write(b"check\r\n")
        lines = ambit_readlines(ser, timeout=2, invalid_bahave=False, max_lines=50)

        adpd_match = re.compile(r"Checking ADPD\s+ADPD Found, chip version: (\d+)")
        as7341_match = re.compile(r"Checking AS7341\s+Success\s+(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
        mlx_match = re.compile(r"Checking MLX90632\s+Success\s+(\d+)\s+([\d.]+)\s+([\d.]+)")
        chan_match = re.compile(r"(\d):\[(\d+),(\d+),(\d+)\]")
        chip_match = re.compile(r"ESP32Temp\s+([\d.]+)")
        light_intensity = 0
        chip_temp = -100.0
        temp1, temp2 = 100.0, 200.0

        for line in lines:
            if adpd_match.match(line):
                ret_dict["ADPD"] = True

            if chip_match.match(line):
                ret = chip_match.findall(line)
                if ret[0][0].isnumeric():
                    chip_temp = float(ret[0])

            if as7341_match.match(line):
                ret = as7341_match.findall(line)
                for n in ret[0]:
                    if n.isnumeric():
                        light_intensity += int(n)
                if light_intensity > 5:
                    ret_dict["AS7341"] = True

            if mlx_match.match(line):
                ret = mlx_match.findall(line)
                read_time = int(ret[0][0])
                temp1 = float(ret[0][1])
                temp2 = float(ret[0][2])
                if read_time < MLX_READ_TIME_LIMIT and temp1 > MIN_TEMP and temp1 < MAX_TEMP and temp2 > MIN_TEMP and temp2 < MAX_TEMP:
                    ret_dict["MLX90632"] = True
                else:
                    if read_time >= MLX_READ_TIME_LIMIT:
                        print("MLX90632 read time too long: {}".format(read_time))
                    else:
                        print("MLX90632 Found, reading time:{}, die temp: {}, object temp: {}".format(ret[0][0], ret[0][1], ret[0][2]))

            if chan_match.match(line):
                ret = chan_match.findall(line)
                ch, d1, l1, l2 = int(ret[0][0]), int(ret[0][1]), int(ret[0][2]), int(ret[0][3])
                if ch == 2:
                    if l1 > d1 + l2:
                        ret_dict["LightPass"] = True
                    else:
                        print("actinic light / adpd failed")

    if abs(chip_temp * 2 - temp1 - temp2) > 30:
        if ret_dict["MLX90632"]:
            print("Temperature reading mismatch, chip temp: {}, mlx temp: {}, {}".format(chip_temp, temp1, temp2))
    else:
        ret_dict["Temp"] = True

    return ret_dict


def main(force_flash=True, run_test=False):
    # Anchor to the script's own directory so the firmware/esptool files are
    # found regardless of the caller's working directory.
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    needs_flash = 0
    missing = [f for f in REQUIRED_FILES if not os.path.exists(f)]
    if missing:
        print(f"[ERROR] Missing required files: {', '.join(missing)}")
        return 1

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
    
    if not force_flash:
        needs_flash, serial_log = detect_invalid_header(port)
        if needs_flash:
            print("\n[WARN] Invalid header detected. Flashing required.")
            flash(port)
            if run_test:
                print("[INFO] Waiting 1 second before running test_ambit()...")
                time.sleep(1)
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
        else:
            if serial_log.strip():
                print("\n[INFO] No 'invalid header' detected; flashing is not required.")
            else:
                print("\n[WARN] No serial output detected during probe; no flash attempt done.")


    if force_flash and not needs_flash:
        print("[INFO] force_flash=true, re-flashing")
        flash(port)
        if run_test:
            print("[INFO] Waiting 5 seconds before running test_ambit()...")
            time.sleep(5)
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
        return 0

    if run_test and not needs_flash:
        print("[INFO] run_test=True, Starting post-flash tests")
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
        return 0
    else:
        print("[INFO] run_test=false, skipping test_ambit()")
        return 0

    


if __name__ == "__main__":
    try:
        # Anchor to the script's own directory so the firmware/esptool files are
        # found regardless of the caller's working directory.
        os.chdir(os.path.dirname(os.path.abspath(__file__)))

        parser = argparse.ArgumentParser(
            description="Flash Ambit firmware using detection or forced mode."
        )

        parser.add_argument(
            "--force-flash",
            nargs="?",
            default="True",
            const="false",
            help="Pass --force-flash=true/false or --force-flash (for true).",
        )

        parser.add_argument(
            "--run-test",
            nargs="?",
            default="false",
            const="true",
            help="Set to true/false to run test_ambit() after successful flash.",
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
