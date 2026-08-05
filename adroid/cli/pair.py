"""`adroid pair` — device pairing subcommands.

Usage:
    adroid pair list
    adroid pair verify [--serial SERIAL]
    adroid pair usb
    adroid pair wireless --ip IP --port PORT --code CODE
    adroid pair connect --ip IP --port PORT
    adroid pair tcpip --ip IP
"""

from __future__ import annotations

import re
import time
from typing import Optional

import typer
from rich.table import Table

from adroid.cli.utils import (
    confirm,
    die,
    error,
    header,
    info,
    run_adb,
    success,
    warn,
    adb_version,
)

app = typer.Typer(
    name="pair",
    help="Pair Android devices via USB, Android 11+ wireless debugging, or tcpip mode.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Regex for "IP:PORT" — used to validate wireless addresses
_IP_PORT_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")
_IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _list_devices() -> list[dict[str, str]]:
    """Return list of {serial, state, type, model, transport_id}."""
    rc, out, _ = run_adb(["devices", "-l"])
    if rc != 0:
        return []
    devices: list[dict[str, str]] = []
    for line in out.splitlines()[1:]:  # skip "List of devices attached"
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        is_wireless = bool(_IP_PORT_RE.match(serial))
        props: dict[str, str] = {}
        for tok in parts[2:]:
            if ":" in tok:
                k, v = tok.split(":", 1)
                props[k] = v
        devices.append({
            "serial": serial,
            "state": state,
            "type": "wireless" if is_wireless else "usb",
            "model": props.get("model", "—"),
            "transport_id": props.get("transport_id", "—"),
        })
    return devices


def _print_devices_table(devices: list[dict[str, str]]) -> None:
    if not devices:
        info("No devices visible.")
        return
    table = Table(show_header=True, header_style="bold cyan", show_lines=False)
    table.add_column("", justify="center", width=2)
    table.add_column("Serial", style="white")
    table.add_column("State", style="white")
    table.add_column("Type", style="white")
    table.add_column("Model", style="white")
    for d in devices:
        icon = "📶" if d["type"] == "wireless" else "🔌"
        state_style = "green" if d["state"] == "device" else (
            "yellow" if d["state"] == "unauthorized" else "red"
        )
        table.add_row(
            icon,
            d["serial"],
            f"[{state_style}]{d['state']}[/{state_style}]",
            d["type"],
            d["model"],
        )
    from adroid.cli.utils import console
    console.print(table)


def _verify_device(serial: Optional[str] = None) -> None:
    """Verify a device is ready for Adroid."""
    header(
        "Device Verification",
        f"Target: {serial or 'auto-pick first ready device'}",
    )

    # ADB version check
    v = adb_version()
    if not v:
        die("adb binary not found or not working.")
    success(f"{v}")

    # List devices
    devices = _list_devices()
    if not devices:
        error("No devices visible. Pair a device first:")
        info("  Android 11+:  adroid pair wireless --ip IP --port PORT --code CODE")
        info("  Android <11:  adroid pair tcpip --ip IP")
        info("  USB:          plug in cable + tap Allow on phone")
        raise typer.Exit(1)

    _print_devices_table(devices)

    # Pick target
    if serial is None:
        ready = [d for d in devices if d["state"] == "device"]
        if not ready:
            error("No device is in 'device' state. Current devices:")
            for d in devices:
                if d["state"] == "unauthorized":
                    info("Phone is showing 'Allow USB debugging?' dialog → tap Allow.")
                elif d["state"] == "offline":
                    info("Device is offline. Try rebooting the phone.")
            raise typer.Exit(1)
        target = ready[0]
        serial = target["serial"]
        success(f"Auto-picked: {serial} ({target['model']}, {target['type']})")
    else:
        target = next((d for d in devices if d["serial"] == serial), None)
        if target is None:
            die(f"Device {serial!r} not found in visible devices.")

    # Shell test
    rc, out, err = run_adb(["-s", serial, "shell", "getprop", "ro.product.model"])
    if rc != 0:
        die(f"Shell command failed: {err.strip()}")
    model = out.strip()
    success(f"Device model: [bold]{model}[/bold]")

    # Android version
    rc, out, _ = run_adb(["-s", serial, "shell", "getprop", "ro.build.version.release"])
    if rc == 0 and out.strip():
        success(f"Android version: {out.strip()}")

    # SDK level
    rc, out, _ = run_adb(["-s", serial, "shell", "getprop", "ro.build.version.sdk"])
    if rc == 0 and out.strip():
        success(f"SDK level: {out.strip()}")

    # Screenshot test
    info("Testing screencap...")
    rc, out, _ = run_adb(["-s", serial, "exec-out", "screencap", "-p"], timeout=15.0)
    # `out` is a string here, but screencap returns binary PNG. We need to re-run to get bytes.
    # Actually run_adb decodes as utf-8 with replace, which corrupts binary. Let's bypass.
    import subprocess
    import shutil as _shutil
    adb_path = _shutil.which("adb") or "adb"
    proc = subprocess.run(
        [adb_path, "-s", serial, "exec-out", "screencap", "-p"],
        capture_output=True,
        timeout=15.0,
        check=False,
    )
    if proc.returncode != 0 or not proc.stdout.startswith(b"\x89PNG"):
        warn(f"screencap did not return PNG (got {len(proc.stdout)} bytes)")
        warn("Screenshot may not work without root on this device.")
    else:
        success(f"screencap works ({len(proc.stdout):,} bytes)")

    # Final message
    from adroid.cli.utils import console
    from rich.panel import Panel
    console.print(Panel(
        f"[bold green]Device ready for Adroid![/bold green]\n\n"
        f"[dim]Start the runtime with:[/dim]\n"
        f"  [cyan]adroid start --bridge adb[/cyan]\n\n"
        f"[dim]Device ID for agent calls:[/dim]\n"
        f'  [white]{{"kind": "adb", "value": "{serial}"}}[/white]'
        + (f"\n\n[dim]Wireless connection active. Phone can be anywhere on this WiFi.[/dim]"
           if target["type"] == "wireless" else ""),
        border_style="green",
        padding=(1, 2),
    ))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command("list")
def list_cmd() -> None:
    """List all connected devices (USB + wireless)."""
    v = adb_version()
    if v:
        info(v)
    header("Connected Devices")
    _print_devices_table(_list_devices())


@app.command("verify")
def verify_cmd(
    serial: Optional[str] = typer.Option(
        None, "--serial", "-s", help="Specific device serial to verify. If omitted, auto-picks first ready device."
    ),
) -> None:
    """Verify a device is ready for Adroid (model, Android version, screencap test)."""
    _verify_device(serial)


@app.command("usb")
def usb_cmd() -> None:
    """Pair via USB cable (classic mode, all Android versions).

    Plug in USB cable, then run this command. Tap 'Allow' on the phone's
    'Allow USB debugging?' dialog.
    """
    header("USB Pairing")
    info("Plug your phone into the laptop via USB cable now.")
    info("Make sure USB debugging is enabled in Developer Options.")
    confirm("Ready to scan for USB devices?", default=True)

    devices = _list_devices()
    usb_devices = [d for d in devices if d["type"] == "usb"]
    if not usb_devices:
        error("No USB devices detected. Current devices:")
        _print_devices_table(devices)
        info("Troubleshooting:")
        info("  - USB cable must be data-capable (not charge-only)")
        info("  - Phone must have USB debugging ON")
        info("  - If phone shows 'Allow USB debugging?' dialog, tap Allow")
        raise typer.Exit(1)

    success(f"USB device detected: {usb_devices[0]['serial']} ({usb_devices[0]['model']})")
    _verify_device(usb_devices[0]["serial"])


@app.command("wireless")
def wireless_cmd(
    ip: str = typer.Option(..., "--ip", help="Phone IP address (e.g. 192.168.1.42)"),
    port: int = typer.Option(..., "--port", help="Pairing port (from 'Pair device with pairing code' screen)"),
    code: str = typer.Option(..., "--code", help="6-digit pairing code shown on phone"),
) -> None:
    """Pair with Android 11+ device using wireless debugging.

    No USB cable needed. On the phone:
      Settings → System → Developer options → Wireless debugging → ON
      → Pair device with pairing code → see IP, port, and 6-digit code

    The phone and laptop must be on the same WiFi network.
    """
    if not _IP_RE.match(ip):
        die(f"Invalid IP address: {ip!r}")
    if not re.match(r"^\d{6}$", code):
        die(f"Pairing code must be 6 digits, got {code!r}")

    host_port = f"{ip}:{port}"
    header("Wireless Pairing (Android 11+)", f"Target: {host_port}")

    rc, out, err = run_adb(["pair", host_port, code], timeout=15.0)
    if rc != 0:
        error(f"Pairing failed: {err.strip()}")
        info("Troubleshooting:")
        info("  1. Phone and laptop must be on the same WiFi network")
        info("  2. Pairing code expires in ~30 seconds — generate a new one if it's stale")
        info("  3. Make sure you're using the PAIRING port (under 'Pair device with pairing code'),")
        info("     not the connect port (top of main wireless debugging screen)")
        raise typer.Exit(1)

    success("Pairing successful!")
    info("")
    info("Now CONNECT using the connect port (different from pairing port).")
    info("On the phone's main Wireless Debugging screen, look at 'IP & port' at the top.")
    info("Then run:")
    info(f"  [cyan]adroid pair connect --ip {ip} --port <CONNECT_PORT>[/cyan]")


@app.command("connect")
def connect_cmd(
    ip: str = typer.Option(..., "--ip", help="Phone IP address"),
    port: int = typer.Option(..., "--port", help="Connect port (top of wireless debugging screen)"),
) -> None:
    """Connect to a previously-paired wireless device."""
    if not _IP_RE.match(ip):
        die(f"Invalid IP address: {ip!r}")

    host_port = f"{ip}:{port}"
    header("Wireless Connect", f"Target: {host_port}")

    rc, out, err = run_adb(["connect", host_port], timeout=10.0)
    if rc != 0 or "connected" not in out.lower():
        error(f"Connect failed: {err.strip() or out.strip()}")
        info("Troubleshooting:")
        info("  1. Make sure Wireless debugging is still ON on the phone")
        info("  2. Make sure phone and laptop are on the same WiFi")
        info("  3. If you just paired, use the CONNECT port (top of wireless debugging screen),")
        info("     not the pairing port (under 'Pair device with pairing code')")
        raise typer.Exit(1)

    success(f"Connected to {host_port}")

    # Show updated device list
    header("Devices After Connect")
    _print_devices_table(_list_devices())


@app.command("tcpip")
def tcpip_cmd(
    ip: str = typer.Option(..., "--ip", help="Phone IP address (e.g. 192.168.1.42)"),
) -> None:
    """One-time USB → wireless transition for Android < 11.

    Flow:
      1. Plug in USB cable.
      2. Adroid runs `adb tcpip 5555` on the phone.
      3. Unplug USB cable.
      4. Adroid connects to PHONE_IP:5555 over WiFi.

    Note: tcpip mode resets when the phone reboots. For Android 11+,
    prefer `adroid pair wireless` instead.
    """
    if not _IP_RE.match(ip):
        die(f"Invalid IP address: {ip!r}")

    header("Wireless Setup (USB → tcpip)", f"Phone IP: {ip}")
    info("This mode is for Android < 11, or when 'Wireless debugging' isn't in Developer options.")
    info("")
    info("Step 1: Plug your phone into the laptop via USB cable now.")
    confirm("Press Enter once the phone is plugged in...", default=True)

    devices = _list_devices()
    usb_devices = [d for d in devices if d["type"] == "usb" and d["state"] == "device"]
    if not usb_devices:
        error("No USB device detected. Current devices:")
        _print_devices_table(devices)
        info("Check: USB cable is data-capable, USB debugging is ON, tap Allow on phone")
        raise typer.Exit(1)

    success(f"USB device detected: {usb_devices[0]['serial']} ({usb_devices[0]['model']})")

    info("")
    info("Step 2: Switching phone to tcpip mode (port 5555)...")
    rc, out, err = run_adb(["tcpip", "5555"], timeout=10.0)
    if rc != 0:
        die(f"tcpip failed: {err.strip()}")
    success(out.strip() or "tcpip mode enabled")

    info("Waiting 2s for phone to start listening...")
    time.sleep(2)

    info("")
    info("Step 3: Unplug the USB cable from the phone.")
    info("        The phone will stay in tcpip mode as long as it's powered on.")
    confirm("Press Enter once unplugged...", default=True)

    info("")
    info("Step 4: Connecting over WiFi...")
    connect_addr = f"{ip}:5555"
    rc, out, err = run_adb(["connect", connect_addr], timeout=10.0)
    if rc != 0 or "connected" not in out.lower():
        die(f"Connect failed: {err.strip() or out.strip()}")

    success(f"Connected to {connect_addr}")
    info("")
    info("Wireless setup complete!")
    info(f"  Phone IP: {ip}")
    info(f"  Connect address: {connect_addr}")
    info("")
    warn("Note: tcpip mode resets when the phone reboots.")
    warn("      For Android 11+ phones, prefer 'adroid pair wireless' instead.")


if __name__ == "__main__":
    app()
