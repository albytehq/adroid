"""Termux-backed DeviceBridge implementation.

Runs inside Termux on an Android phone. Uses the ``termux-api`` CLI
package to access device capabilities:

    - ``termux-screenshot``      → PNG bytes
    - ``termux-toast``           → popup notification
    - ``termux-notification``    → status bar notification
    - ``termux-battery-status``  → battery info JSON
    - ``termux-telephony-deviceinfo`` → device info JSON
    - ``pm list packages``       → installed apps (no root needed)
    - ``am start``               → launch app (no root needed)
    - ``input tap/text``         → input injection (requires android.permission.INJECT_EVENTS, rarely works without root)

Requirements on the phone:
    1. Termux installed (from F-Droid — Play Store version is deprecated)
    2. Termux:API installed (com.termux.api)
    3. ``pkg install termux-api python`` inside Termux
    4. ``pip install adroid[termux]``

The bridge does NOT need root for read-only operations (screenshot, list
packages, battery). Input injection (tap, text) typically requires root
or a signed system app — v0.1.0 ships those methods but they may fail
on most non-rooted devices. v0.2.0 will land an accessibility-service
bridge for non-rooted input injection.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

from adroid.bridge.base import BlobStore
from adroid.contract.errors import BridgeError, DeviceNotAvailableError
from adroid.contract.types import AppInfo, DeviceId, DeviceInfo, DeviceState, Screenshot


class TermuxBridge:
    """DeviceBridge implementation that runs INSIDE Termux on the phone.

    Unlike ``AdbBridge`` (which talks to a device over USB/network from a
    host), this bridge IS the device. The single ``DeviceId`` it reports
    is the phone itself — there is no serial number to discover.

    The bridge is constructed with a fixed ``device_id`` (typically
    ``DeviceId(kind="termux", value="self")``) and every method targets
    that device.
    """

    def __init__(
        self,
        *,
        blob_store: BlobStore,
        device_id: DeviceId | None = None,
        command_timeout: float = 30.0,
    ):
        self._blob_store = blob_store
        self._device_id = device_id or DeviceId(kind="termux", value="self")
        self._timeout = command_timeout

    @property
    def device_id(self) -> DeviceId:
        return self._device_id

    # ------------------------------------------------------------------
    # DeviceBridge Protocol
    # ------------------------------------------------------------------

    def list_devices(self) -> list[DeviceInfo]:
        """Termux bridge always reports exactly one device: itself."""
        return [self._probe_device_info()]

    def get_device_info(self, device_id: DeviceId) -> DeviceInfo:
        if device_id != self._device_id:
            raise DeviceNotAvailableError(
                f"TermuxBridge only targets {self._device_id}, got {device_id}",
                code="adroid.bridge.termux.device_mismatch",
                details={"expected": str(self._device_id), "got": str(device_id)},
            )
        return self._probe_device_info()

    def list_installed_apps(self, device_id: DeviceId) -> list[AppInfo]:
        self._require_match(device_id)
        # `pm list packages` works in Termux without root. Returns lines
        # like "package:com.example.app".
        raw = self._run(["pm", "list", "packages", "-3"])  # -3 = third-party only
        apps: list[AppInfo] = []
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("package:"):
                pkg = line[len("package:"):].strip()
                if pkg:
                    apps.append(AppInfo(package_name=pkg, system_app=False))
        return apps

    def capture_screenshot(self, device_id: DeviceId) -> Screenshot:
        self._require_match(device_id)
        # termux-screenshot writes to a file. We pick a temp path and read it.
        # In Termux, $TMPDIR is usually /data/data/com.termux/files/usr/tmp.
        import os
        import tempfile

        tmp_dir = tempfile.gettempdir()
        out_path = os.path.join(tmp_dir, f"adroid-shot-{os.getpid()}.png")
        try:
            self._run(["termux-screenshot", out_path])
            with open(out_path, "rb") as f:
                png_bytes = f.read()
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

        if not png_bytes or not png_bytes.startswith(b"\x89PNG"):
            raise BridgeError(
                "termux-screenshot did not produce valid PNG data",
                code="adroid.bridge.termux.screenshot_invalid",
            )

        blob_ref = self._blob_store.put(png_bytes)
        width, height = self._parse_png_dimensions(png_bytes)
        return Screenshot(
            device_id=device_id,
            blob_ref=blob_ref,
            width=width,
            height=height,
        )

    def launch_app(self, device_id: DeviceId, package_name: str) -> None:
        self._require_match(device_id)
        # Resolve launcher activity then am start. Works without root.
        raw = self._run(
            ["cmd", "package", "resolve-activity", "--brief", package_name]
        )
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        component = lines[-1] if lines else None
        if not component or "/" not in component:
            raise BridgeError(
                f"could not resolve launcher activity for {package_name}",
                code="adroid.bridge.termux.no_launcher_activity",
                details={"package_name": package_name},
            )
        self._run(["am", "start", "-n", component])

    def force_stop_app(self, device_id: DeviceId, package_name: str) -> None:
        self._require_match(device_id)
        self._run(["am", "force-stop", package_name])

    def tap(self, device_id: DeviceId, x: int, y: int) -> None:
        """Inject a tap. Likely fails on non-rooted devices — needs
        ``android.permission.INJECT_EVENTS`` which is system-app only.
        v0.2.0 will use accessibility service for this.
        """
        self._require_match(device_id)
        self._run(["input", "tap", str(x), str(y)])

    def input_text(self, device_id: DeviceId, text: str) -> None:
        self._require_match(device_id)
        escaped = text.replace(" ", "%s")
        self._run(["input", "text", escaped])

    def press_key(self, device_id: DeviceId, keycode: str) -> None:
        self._require_match(device_id)
        self._run(["input", "keyevent", keycode])

    def tap_text(
        self,
        device_id: DeviceId,
        text: str,
        *,
        match: str = "first",
        scroll_to_visible: bool = True,
    ) -> dict:
        """Tap an element by visible text. Uses uiautomator dump.

        In Termux, `uiautomator` is available without root (it's a system
        binary). The dump+tap flow is the same as AdbBridge.
        """
        import time

        self._require_match(device_id)
        start = time.monotonic()
        scrolled = False
        attempts = 0
        max_scroll_attempts = 5 if scroll_to_visible else 1

        while attempts < max_scroll_attempts:
            dump_path = "/sdcard/adroid-ui-dump.xml"
            self._run(["uiautomator", "dump", dump_path])
            # Read the file directly (Termux can access /sdcard with permission)
            try:
                with open(dump_path, "r", encoding="utf-8") as f:
                    xml_raw = f.read()
            except OSError as exc:
                raise BridgeError(
                    f"failed to read uiautomator dump: {exc}",
                    code="adroid.bridge.termux.ui_dump_read_error",
                ) from exc
            finally:
                try:
                    self._run(["rm", dump_path])
                except BridgeError:
                    pass

            # Parse and find matches (re-use AdbBridge's static helpers)
            from adroid.bridge.adb import AdbBridge
            matches = AdbBridge._find_elements_by_text(xml_raw, text)
            if matches:
                if match == "last":
                    target = matches[-1]
                elif match == "best":
                    target = max(matches, key=lambda e: len(e.get("text", "")))
                else:
                    target = matches[0]

                bounds = target["bounds"]
                cx = bounds["x"] + bounds["w"] // 2
                cy = bounds["y"] + bounds["h"] // 2
                self.tap(device_id, cx, cy)

                duration_ms = int((time.monotonic() - start) * 1000)
                return {
                    "matched_elements": len(matches),
                    "tapped_element": {"text": target["text"], "bounds": bounds},
                    "scrolled": scrolled,
                    "duration_ms": duration_ms,
                }

            if scroll_to_visible and attempts < max_scroll_attempts - 1:
                self._run(["input", "swipe", "540", "1800", "540", "600", "300"])
                scrolled = True
                time.sleep(0.5)

            attempts += 1

        raise BridgeError(
            f"no element found with text={text!r} after {attempts} attempt(s)",
            code="adroid.bridge.termux.tap_text_not_found",
            details={"text": text, "match": match, "scrolled": scrolled},
        )

    def swipe(
        self,
        device_id: DeviceId,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        *,
        duration_ms: int = 300,
    ) -> None:
        self._require_match(device_id)
        self._run([
            "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration_ms),
        ])

    def run_shell(self, device_id: DeviceId, command: str) -> dict:
        """Execute an allowlisted shell command. We're already ON the device
        in Termux mode, so just run the command directly via subprocess."""
        import subprocess
        import time
        self._require_match(device_id)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                f"shell command timed out: {command}",
                code="adroid.bridge.termux.shell_timeout",
                details={"command": command},
            ) from exc

        duration_ms = int((time.monotonic() - start) * 1000)
        return {
            "stdout": proc.stdout.decode("utf-8", "replace"),
            "stderr": proc.stderr.decode("utf-8", "replace"),
            "returncode": proc.returncode,
            "duration_ms": duration_ms,
        }

    def read_logs(
        self,
        device_id: DeviceId,
        *,
        lines: int = 100,
        filter: str | None = None,
    ) -> dict:
        """Read logcat directly (we're on the device in Termux mode)."""
        import subprocess
        self._require_match(device_id)
        args = ["logcat", "-d", "-t", str(lines)]
        if filter:
            args.extend(filter.split())

        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                "logcat timed out",
                code="adroid.bridge.termux.logcat_timeout",
            ) from exc
        except FileNotFoundError as exc:
            raise BridgeError(
                "logcat binary not found (not running on Android?)",
                code="adroid.bridge.termux.logcat_missing",
            ) from exc

        raw = proc.stdout.decode("utf-8", "replace")
        all_lines = raw.splitlines()
        result_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {
            "lines": result_lines,
            "truncated": len(all_lines) > lines,
            "total_bytes": sum(len(l.encode("utf-8")) for l in result_lines),
        }

    # ------------------------------------------------------------------
    # Termux-specific extras (not part of DeviceBridge Protocol)
    # ------------------------------------------------------------------

    def toast(self, message: str) -> None:
        """Show a toast popup on the phone. Useful for the runtime to
        notify the user that an agent is requesting permission."""
        self._run(["termux-toast", message])

    def notify(self, *, title: str, content: str) -> None:
        """Post a status bar notification."""
        self._run(["termux-notification", "--title", title, "--content", content])

    def battery_status(self) -> dict[str, Any]:
        raw = self._run(["termux-battery-status"])
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BridgeError(
                f"termux-battery-status returned invalid JSON: {exc}",
                code="adroid.bridge.termux.battery_invalid",
            ) from exc

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_match(self, device_id: DeviceId) -> None:
        if device_id != self._device_id:
            raise DeviceNotAvailableError(
                f"TermuxBridge only targets {self._device_id}, got {device_id}",
                code="adroid.bridge.termux.device_mismatch",
            )

    def _probe_device_info(self) -> DeviceInfo:
        """Best-effort device info via termux-telephony-deviceinfo + battery."""
        state = DeviceState.ONLINE  # if we're running, we're online
        model: str | None = None
        manufacturer: str | None = None
        android_version: str | None = None
        sdk_level: int | None = None
        battery: float | None = None

        # Try getprop for model + manufacturer + version (no root needed)
        try:
            model = self._run_getprop("ro.product.model")
            manufacturer = self._run_getprop("ro.product.manufacturer")
            android_version = self._run_getprop("ro.build.version.release")
            sdk_str = self._run_getprop("ro.build.version.sdk")
            if sdk_str:
                sdk_level = int(sdk_str)
        except BridgeError:
            pass  # getprop not available? fall back to None

        # Battery via termux-api
        try:
            batt = self.battery_status()
            battery = float(batt.get("percentage"))
        except (BridgeError, ValueError, TypeError):
            pass

        return DeviceInfo(
            id=self._device_id,
            state=state,
            model=model,
            manufacturer=manufacturer,
            android_version=android_version,
            sdk_level=sdk_level,
            battery_level=battery,
        )

    def _run_getprop(self, key: str) -> str | None:
        try:
            return self._run(["getprop", key]).strip() or None
        except BridgeError:
            return None

    def _run(self, cmd: list[str]) -> str:
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise BridgeError(
                f"command not found: {cmd[0]} (is termux-api installed?)",
                code="adroid.bridge.termux.cmd_missing",
                details={"cmd": cmd[0]},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                f"command timed out: {' '.join(cmd)}",
                code="adroid.bridge.termux.timeout",
            ) from exc

        if proc.returncode != 0:
            raise BridgeError(
                f"command failed: {' '.join(cmd)}",
                code="adroid.bridge.termux.command_failed",
                details={
                    "returncode": proc.returncode,
                    "stderr": proc.stderr.decode("utf-8", "replace")[:2048],
                },
            )
        return proc.stdout.decode("utf-8", "replace")

    @staticmethod
    def _parse_png_dimensions(data: bytes) -> tuple[int, int]:
        if len(data) < 24:
            raise BridgeError(
                "PNG too short to extract dimensions",
                code="adroid.bridge.termux.png_truncated",
            )
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height)

    @staticmethod
    def is_termux() -> bool:
        """Detect whether we're running inside Termux."""
        import os
        return "com.termux" in os.environ.get("PREFIX", "")
