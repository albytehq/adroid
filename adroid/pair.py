#!/usr/bin/env python
"""ADB device pairing helper for Adroid.

Supports three pairing modes:

  1. USB (the classic):
         Plug in USB cable → tap Allow on phone → done.

  2. Android 11+ Wireless Debugging (no USB needed at all):
         On phone: Settings → Developer options → Wireless debugging ON
                   → Pair device with pairing code → see HOST:PORT + 6-digit code
         On laptop: adroid-pair --pair 192.168.1.42:4321 --code 123456

  3. WiFi ADB via tcpip (Android < 11, or when wireless debugging unavailable):
         One-time USB to enable tcpip mode, then wireless forever after.
         adroid-pair --wireless-setup 192.168.1.42
         (This plugs in USB briefly, runs `adb tcpip 5555`, then unplugs
          and connects over WiFi.)

After pairing (any mode), connect explicitly with:
         adroid-pair --connect 192.168.1.42:5555

List currently visible devices:
         adroid-pair --list

Verify a device is ready for Adroid:
         adroid-pair --verify 192.168.1.42:5555
         adroid-pair --verify            # auto-pick first device

Quick mode (assumes you know what you're doing):
         adroid-pair                     # list + auto-verify
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from typing import Any


# Regex for "IP:PORT" — used to validate wireless addresses
IP_PORT_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")


def find_adb(adb_path: str | None) -> str:
    path = adb_path or shutil.which("adb") or "adb"
    try:
        proc = subprocess.run([path, "version"], capture_output=True, timeout=5, check=False)
        if proc.returncode != 0:
            print(f"ERROR: adb at {path!r} returned non-zero exit.", file=sys.stderr)
            sys.exit(2)
        version_line = proc.stdout.decode("utf-8", "replace").splitlines()[0]
        print(f"[ok] {version_line}")
        return path
    except FileNotFoundError:
        print(f"ERROR: adb binary not found at {path!r}.", file=sys.stderr)
        print("Install via:", file=sys.stderr)
        print("  Debian/Ubuntu: sudo apt install android-tools-adb", file=sys.stderr)
        print("  macOS:         brew install android-tools", file=sys.stderr)
        print("  Windows:       download platform-tools from developer.android.com", file=sys.stderr)
        sys.exit(2)


def _run_adb(adb: str, args: list[str], *, timeout: float = 15.0) -> tuple[int, str, str]:
    """Run an adb command, return (returncode, stdout, stderr) as strings."""
    proc = subprocess.run([adb] + args, capture_output=True, timeout=timeout, check=False)
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def list_devices(adb: str) -> list[dict[str, str]]:
    """Return a list of {serial, state, transport, type, model} for each device."""
    rc, out, _ = _run_adb(adb, ["devices", "-l"])
    devices: list[dict[str, str]] = []
    for line in out.splitlines()[1:]:  # skip "List of devices attached"
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        # Detect type: wireless devices have IP:PORT format
        is_wireless = bool(IP_PORT_RE.match(serial))
        props = {}
        for tok in parts[2:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                props[k] = v
        devices.append({
            "serial": serial,
            "state": state,
            "type": "wireless" if is_wireless else "usb",
            "model": props.get("model", "?"),
            "transport_id": props.get("transport_id", "?"),
        })
    return devices


def print_devices(devices: list[dict[str, str]]) -> None:
    if not devices:
        print("  (no devices visible)")
        return
    for d in devices:
        icon = "📶" if d["type"] == "wireless" else "🔌"
        print(f"  {icon} {d['serial']:30s} [{d['state']:12s}] {d['model']} ({d['type']})")


def pair_wireless(adb: str, host_port: str, code: str) -> None:
    """Pair with an Android 11+ device using a 6-digit pairing code.

    On the phone: Settings → System → Developer options → Wireless debugging
    → Pair device with pairing code. The phone shows HOST:PORT and a 6-digit code.
    """
    if not IP_PORT_RE.match(host_port):
        print(f"ERROR: {host_port!r} is not a valid HOST:PORT (expected e.g. 192.168.1.42:4321)", file=sys.stderr)
        sys.exit(2)
    if not re.match(r"^\d{6}$", code):
        print(f"ERROR: pairing code must be 6 digits, got {code!r}", file=sys.stderr)
        sys.exit(2)

    print(f"\n=== Pairing with {host_port} (code={code}) ===")
    rc, out, err = _run_adb(adb, ["pair", host_port, code], timeout=15.0)
    print(out.strip())
    if rc != 0:
        print(f"[error] pairing failed: {err.strip()}", file=sys.stderr)
        print(file=sys.stderr)
        print("Troubleshooting:", file=sys.stderr)
        print("  1. Make sure the phone and laptop are on the same WiFi network.", file=sys.stderr)
        print("  2. Make sure the pairing code hasn't expired (it's only valid ~30s).", file=sys.stderr)
        print("  3. Make sure you're using the 'pairing' port, not the 'connect' port.", file=sys.stderr)
        print("     (Pairing port is shown under 'Pair device with pairing code'.)", file=sys.stderr)
        sys.exit(1)

    print("\n[ok] pairing successful")
    print("Now you need to CONNECT (the pairing port is different from the connect port).")
    print("On the phone's main Wireless Debugging screen, look at the 'IP & port' line at the top.")
    print("That's the connect address. Use:")
    print(f"  adroid-pair --connect <IP>:<PORT>")


def connect_wireless(adb: str, host_port: str) -> None:
    """Connect to a previously-paired wireless device."""
    if not IP_PORT_RE.match(host_port):
        print(f"ERROR: {host_port!r} is not a valid HOST:PORT", file=sys.stderr)
        sys.exit(2)

    print(f"\n=== Connecting to {host_port} ===")
    rc, out, err = _run_adb(adb, ["connect", host_port], timeout=10.0)
    print(out.strip())
    if rc != 0 or "connected" not in out.lower():
        print(f"[error] connect failed: {err.strip()}", file=sys.stderr)
        print(file=sys.stderr)
        print("Troubleshooting:", file=sys.stderr)
        print("  1. Make sure the phone is on and Wireless debugging is enabled.", file=sys.stderr)
        print("  2. Make sure the phone and laptop are on the same WiFi network.", file=sys.stderr)
        print("  3. If you just paired, make sure you're using the 'connect' port (top of screen),", file=sys.stderr)
        print("     not the 'pairing' port (under 'Pair device with pairing code').", file=sys.stderr)
        sys.exit(1)

    print(f"[ok] connected to {host_port}")


def wireless_setup(adb: str, phone_ip: str) -> None:
    """One-time USB → wireless transition for Android < 11 (or when wireless
    debugging isn't available).

    Flow:
        1. User plugs in USB cable.
        2. We run `adb tcpip 5555` — this tells the phone to listen on port 5555.
        3. User unplugs USB cable.
        4. We connect to PHONE_IP:5555.
        5. Wireless works forever after (until phone reboot, which resets tcpip mode).

    For Android 11+ devices, prefer `--pair` instead — it doesn't need USB at all.
    """
    if not re.match(r"^\d{1,3}(?:\.\d{1,3}){3}$", phone_ip):
        print(f"ERROR: {phone_ip!r} is not a valid IPv4 address", file=sys.stderr)
        sys.exit(2)

    print("\n=== Wireless Setup (USB → tcpip mode) ===")
    print()
    print("This mode is for Android < 11, or when 'Wireless debugging' isn't in Developer options.")
    print()
    print("Step 1: Plug your phone into the laptop via USB cable now.")
    print("        (Make sure USB debugging is enabled in Developer options.)")
    input("        Press Enter once the phone is plugged in... ")

    # Verify USB device is visible
    devices = list_devices(adb)
    usb_devices = [d for d in devices if d["type"] == "usb" and d["state"] == "device"]
    if not usb_devices:
        print(f"[error] no USB device detected. Current devices:", file=sys.stderr)
        print_devices(devices)
        print(file=sys.stderr)
        print("Check:", file=sys.stderr)
        print("  - USB cable is data-capable (not charge-only)", file=sys.stderr)
        print("  - Phone has USB debugging ON", file=sys.stderr)
        print("  - If phone shows 'Allow USB debugging?' dialog, tap Allow", file=sys.stderr)
        sys.exit(1)

    print(f"[ok] USB device detected: {usb_devices[0]['serial']} ({usb_devices[0]['model']})")

    # Run adb tcpip 5555
    print("\nStep 2: Switching phone to tcpip mode (port 5555)...")
    rc, out, err = _run_adb(adb, ["tcpip", "5555"], timeout=10.0)
    print(out.strip())
    if rc != 0:
        print(f"[error] tcpip failed: {err.strip()}", file=sys.stderr)
        sys.exit(1)

    # Give the phone a moment to start listening
    import time
    print("  (waiting 2s for phone to start listening...)")
    time.sleep(2)

    print("\nStep 3: Unplug the USB cable from the phone.")
    print("        The phone will stay in tcpip mode as long as it's powered on.")
    input("        Press Enter once unplugged... ")

    # Connect over WiFi
    connect_addr = f"{phone_ip}:5555"
    print(f"\nStep 4: Connecting to {connect_addr}...")
    connect_wireless(adb, connect_addr)

    print("\n[ok] wireless setup complete!")
    print(f"  Phone IP: {phone_ip}")
    print(f"  Connect address: {connect_addr}")
    print()
    print("Note: tcpip mode resets when the phone reboots.")
    print("      Re-run this command after a reboot to re-enable wireless.")
    print()
    print("For Android 11+ phones, prefer 'adroid-pair --pair' instead —")
    print("it survives reboots and doesn't need USB at all.")


def verify_device(adb: str, serial: str | None = None) -> None:
    """Verify a device is ready: shows up, is authorized, screencap works."""
    print(f"\n=== Verifying device {serial or '(auto-pick first)'} ===")
    devices = list_devices(adb)
    if not devices:
        print("[error] no devices visible. Pair a device first:", file=sys.stderr)
        print("  Android 11+:  adroid-pair --pair HOST:PORT --code 123456", file=sys.stderr)
        print("  Android <11:  adroid-pair --wireless-setup PHONE_IP", file=sys.stderr)
        print("  USB:          plug in cable + tap Allow on phone", file=sys.stderr)
        sys.exit(1)

    if serial is None:
        # Auto-pick first device that's in 'device' state
        ready = [d for d in devices if d["state"] == "device"]
        if not ready:
            print(f"[error] no device is in 'device' state. Current devices:", file=sys.stderr)
            print_devices(devices)
            print(file=sys.stderr)
            print("If state is 'unauthorized', tap 'Allow' on the phone's USB debugging dialog.", file=sys.stderr)
            print("If state is 'offline', the device may need a reboot.", file=sys.stderr)
            sys.exit(1)
        target = ready[0]
        serial = target["serial"]
        print(f"[ok] auto-picked: {serial} ({target['model']}, {target['type']})")
    else:
        target = next((d for d in devices if d["serial"] == serial), None)
        if target is None:
            print(f"[error] device {serial!r} not found. Visible devices:", file=sys.stderr)
            print_devices(devices)
            sys.exit(1)

    # Shell test
    rc, out, err = _run_adb(adb, ["-s", serial, "shell", "getprop", "ro.product.model"])
    if rc != 0:
        print(f"[error] shell command failed: {err.strip()}", file=sys.stderr)
        if "unauthorized" in err.lower():
            print("\nThe phone is showing an 'Allow USB debugging?' dialog.", file=sys.stderr)
            print("Tap 'Allow' (and check 'Always allow from this computer').", file=sys.stderr)
        sys.exit(1)

    model = out.strip()
    print(f"[ok] device model: {model}")

    # Android version
    rc, out, _ = _run_adb(adb, ["-s", serial, "shell", "getprop", "ro.build.version.release"])
    if rc == 0:
        print(f"[ok] Android version: {out.strip()}")

    # Screenshot test
    print("[ok] testing screencap...")
    rc, out, _ = _run_adb(adb, ["-s", serial, "exec-out", "screencap", "-p"], timeout=15.0)
    if rc != 0 or not out.startswith("\x89PNG".encode()):
        print(f"[warn] screencap did not return PNG (got {len(out)} bytes)", file=sys.stderr)
    else:
        print(f"[ok] screencap works ({len(out)} bytes)")

    print()
    print("=" * 60)
    print("Device ready for Adroid!")
    print("=" * 60)
    print()
    print("Start the runtime:")
    print(f"  adroid-start --bridge adb --port 7654")
    print()
    print("Device ID to use in agent calls:")
    print(f'  {{"kind": "adb", "value": "{serial}"}}')
    print()
    if target["type"] == "wireless":
        print("Wireless connection active. Phone can be anywhere on this WiFi network.")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="adroid-pair",
        description="ADB device pairing helper — USB, Android 11+ wireless pairing, or tcpip mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List currently visible devices
  adroid-pair --list

  # Android 11+ wireless pairing (no USB needed)
  adroid-pair --pair 192.168.1.42:4321 --code 123456
  adroid-pair --connect 192.168.1.42:5555

  # Android < 11 wireless setup (USB once, then wireless forever)
  adroid-pair --wireless-setup 192.168.1.42

  # Verify a device is ready
  adroid-pair --verify
  adroid-pair --verify 192.168.1.42:5555

  # Plain USB — just plug in cable and run:
  adroid-pair
        """,
    )
    parser.add_argument("--adb-path", default=None, help="Path to adb binary. Defaults to PATH lookup.")
    parser.add_argument("--list", action="store_true", help="List connected devices (USB + wireless).")
    parser.add_argument("--pair", metavar="HOST:PORT", default=None,
                        help="Pair with Android 11+ device using --code (no USB needed).")
    parser.add_argument("--code", default=None, help="6-digit pairing code for --pair.")
    parser.add_argument("--connect", metavar="HOST:PORT", default=None,
                        help="Connect to a previously-paired wireless device.")
    parser.add_argument("--wireless-setup", metavar="PHONE_IP", default=None,
                        help="One-time USB → wireless transition for Android < 11.")
    parser.add_argument("--verify", nargs="?", const="", default=None,
                        help="Verify device is ready for Adroid. Pass optional serial, or omit for auto-pick.")
    parser.add_argument("--serial", default=None, help="(Deprecated, use --verify SERIAL) Device serial to verify.")
    args = parser.parse_args()

    adb = find_adb(args.adb_path)

    if args.list:
        print("\n=== Connected devices ===")
        print_devices(list_devices(adb))
        return

    if args.pair:
        if not args.code:
            print("ERROR: --pair requires --code", file=sys.stderr)
            sys.exit(2)
        pair_wireless(adb, args.pair, args.code)
        # List after pairing
        print("\n=== Devices after pairing ===")
        print_devices(list_devices(adb))
        return

    if args.connect:
        connect_wireless(adb, args.connect)
        print("\n=== Devices after connect ===")
        print_devices(list_devices(adb))
        return

    if args.wireless_setup:
        wireless_setup(adb, args.wireless_setup)
        return

    if args.verify is not None:
        # --verify with optional serial arg
        serial = args.verify if args.verify else None
        verify_device(adb, serial)
        return

    # Legacy: --serial flag
    if args.serial:
        verify_device(adb, args.serial)
        return

    # Default: list + auto-verify
    print("\n=== Connected devices ===")
    devs = list_devices(adb)
    print_devices(devs)
    print()
    verify_device(adb, None)


if __name__ == "__main__":
    main()
