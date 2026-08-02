#!/usr/bin/env python
"""ADB device pairing helper for Adroid.

Helps the user:
    1. Detect ADB availability
    2. List connected devices (USB or WiFi)
    3. Pair with a wireless device (Android 11+)
    4. Verify the device is ready for Adroid to use

Run with::

    python -m adroid.cli pair              # interactive
    python -m adroid.cli pair --list       # just list devices
    python -m adroid.cli pair --wireless 192.168.1.42:5555
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys


def find_adb(adb_path: str | None) -> str:
    path = adb_path or shutil.which("adb") or "adb"
    # Verify it actually works
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


def list_devices(adb: str) -> None:
    print("\n=== Connected devices ===")
    proc = subprocess.run([adb, "devices", "-l"], capture_output=True, timeout=5, check=False)
    print(proc.stdout.decode("utf-8", "replace"))
    if proc.returncode != 0:
        print(f"[error] adb returned {proc.returncode}", file=sys.stderr)
        print(proc.stderr.decode("utf-8", "replace"), file=sys.stderr)


def pair_wireless(adb: str, host_port: str, code: str | None) -> None:
    """Pair with an Android 11+ device over WiFi.

    On the device: Settings → Developer options → Wireless debugging →
    Pair device with pairing code. The phone shows an IP:port and a 6-digit code.
    """
    print(f"\n=== Pairing with {host_port} ===")
    if not code:
        print("On your phone, enable Wireless Debugging, tap 'Pair device with pairing code',")
        print("and copy the pairing code (6 digits) shown on screen.")
        code = input("Enter pairing code: ").strip()
        if not code:
            print("No code entered, aborting.", file=sys.stderr)
            sys.exit(2)

    proc = subprocess.run(
        [adb, "pair", host_port, code],
        capture_output=True,
        timeout=10,
        check=False,
        input=None,
    )
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    print(out)
    if proc.returncode != 0:
        print(f"[error] pairing failed: {err}", file=sys.stderr)
        sys.exit(1)

    # After pairing, we need to connect (pairing port ≠ connect port)
    # The phone shows a separate "IP & port" for connecting in the wireless debugging screen.
    print("\nPairing complete! Now connect using the IP & port shown on the main wireless debugging screen:")
    connect_addr = input("Enter IP:port to connect: ").strip()
    if not connect_addr:
        print("No connect address entered.", file=sys.stderr)
        sys.exit(2)

    proc = subprocess.run(
        [adb, "connect", connect_addr],
        capture_output=True,
        timeout=10,
        check=False,
    )
    print(proc.stdout.decode("utf-8", "replace"))
    if proc.returncode != 0:
        print(f"[error] connect failed: {proc.stderr.decode('utf-8', 'replace')}", file=sys.stderr)
        sys.exit(1)


def verify_device(adb: str, serial: str | None = None) -> None:
    """Verify a device is ready: shows up, is authorized, and basic shell works."""
    print(f"\n=== Verifying device {serial or '(any)'} ===")
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.append("shell")
    cmd.extend(["getprop", "ro.product.model"])

    proc = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        print(f"[error] shell command failed: {err}", file=sys.stderr)
        if "unauthorized" in err.lower():
            print("\nThe phone is showing an 'Allow USB debugging?' dialog.", file=sys.stderr)
            print("Tap 'Allow' (and optionally 'Always allow from this computer') on the phone screen.", file=sys.stderr)
        sys.exit(1)

    model = proc.stdout.decode("utf-8", "replace").strip()
    print(f"[ok] device model: {model}")

    # Test screenshot capability
    print("[ok] testing screencap...")
    cmd = [adb]
    if serial:
        cmd.extend(["-s", serial])
    cmd.extend(["exec-out", "screencap", "-p"])
    proc = subprocess.run(cmd, capture_output=True, timeout=15, check=False)
    if proc.returncode != 0 or not proc.stdout.startswith(b"\x89PNG"):
        print(f"[warn] screencap did not return PNG (got {len(proc.stdout)} bytes)", file=sys.stderr)
        print("       Screenshot may not work without root on this device.", file=sys.stderr)
    else:
        print(f"[ok] screencap works ({len(proc.stdout)} bytes)")

    print(f"\nDevice ready for Adroid!")
    print(f"Start runtime with:")
    print(f"  adroid-start --bridge adb --port 7654 --adb-path {adb}")
    print(f"Device ID for agent calls: {{\"kind\": \"adb\", \"value\": \"{serial or '<auto>\"'}}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="adroid-pair",
        description="ADB device pairing + verification helper.",
    )
    parser.add_argument("--adb-path", default=None, help="Path to adb binary. Defaults to PATH lookup.")
    parser.add_argument("--list", action="store_true", help="Just list connected devices.")
    parser.add_argument("--wireless", metavar="HOST:PORT", default=None, help="Pair with a wireless device (Android 11+).")
    parser.add_argument("--code", default=None, help="Pairing code (6 digits). If omitted, prompts interactively.")
    parser.add_argument("--serial", default=None, help="Device serial to verify. If omitted, verifies the first one.")
    args = parser.parse_args()

    adb = find_adb(args.adb_path)

    if args.list:
        list_devices(adb)
        return

    if args.wireless:
        pair_wireless(adb, args.wireless, args.code)
        list_devices(adb)
        # Auto-verify the just-paired device if --serial not given
        # (user can re-run with --serial to verify a specific one)
        return

    # Default: list + verify
    list_devices(adb)
    verify_device(adb, args.serial)


if __name__ == "__main__":
    main()
