"""ADB-backed DeviceBridge implementation.

This is the only implementation that talks to a real device. It shells
out to the ``adb`` binary — we deliberately do NOT bundle the ADB Java
library, because:
    1. The adb binary is the most stable Android SDK surface (it has not
       had a breaking change in 10+ years).
    2. Shelling out isolates us from JVM version drift on integrator hosts.
    3. It is trivially mockable in tests by overriding ``_run_adb``.

Trade-off: subprocess overhead (~5ms per call on Linux). For v0.1.0
observation-heavy workloads this is fine. v0.2.0 will land a persistent
ADB server connection for high-frequency polling.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from typing import Any

from adroid.bridge.base import BlobStore
from adroid.contract.errors import BridgeError, DeviceNotAvailableError
from adroid.contract.types import AppInfo, DeviceId, DeviceInfo, DeviceState, Screenshot


class AdbBridge:
    """DeviceBridge implementation that drives the ``adb`` binary."""

    def __init__(
        self,
        *,
        adb_path: str | None = None,
        blob_store: BlobStore,
        adb_host: str | None = None,
        command_timeout: float = 15.0,
    ):
        self._adb_path = adb_path or shutil.which("adb") or "adb"
        self._blob_store = blob_store
        self._adb_host = adb_host
        self._timeout = command_timeout

    # ------------------------------------------------------------------
    # Public DeviceBridge API
    # ------------------------------------------------------------------

    def list_devices(self) -> list[DeviceInfo]:
        raw = self._run_adb(["devices", "-l"])
        return self._parse_devices_output(raw)

    def get_device_info(self, device_id: DeviceId) -> DeviceInfo:
        if device_id.kind != "adb":
            raise DeviceNotAvailableError(
                f"AdbBridge cannot target device of kind '{device_id.kind}'",
                code="adroid.bridge.kind_mismatch",
                details={"device_id": str(device_id)},
            )
        # Re-list and pick ours. Cheaper than a separate getprop round-trip
        # for v0.1.0; v0.2.0 will cache per-serial getprop output.
        for d in self.list_devices():
            if d.id.value == device_id.value:
                return d
        raise DeviceNotAvailableError(
            f"device not visible to bridge: {device_id}",
            code="adroid.bridge.device_not_visible",
            details={"device_id": str(device_id)},
        )

    def list_installed_apps(self, device_id: DeviceId) -> list[AppInfo]:
        serial = self._require_adb_serial(device_id)
        raw = self._run_adb(["-s", serial, "shell", "pm", "list", "packages", "-s", "-3"])
        # -s = system apps only would be wrong; we want both. Use -3 (third-party)
        # plus a separate call for system apps. For v0.1.0 simplicity we list ALL
        # packages via `pm list packages` (no flag).
        raw = self._run_adb(["-s", serial, "shell", "pm", "list", "packages"])
        return self._parse_packages_output(raw, system_flag=False)

    def capture_screenshot(self, device_id: DeviceId) -> Screenshot:
        serial = self._require_adb_serial(device_id)
        # Pull screenshot via exec-out — avoids writing to /sdcard. PNG bytes
        # stream back over stdout. This is the cleanest path on modern Android.
        try:
            proc = subprocess.run(
                self._adb_cmd(["-s", serial, "exec-out", "screencap", "-p"]),
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                "screencap timed out",
                code="adroid.bridge.screenshot_timeout",
            ) from exc

        if proc.returncode != 0:
            raise BridgeError(
                "screencap failed",
                code="adroid.bridge.screenshot_failed",
                details={"returncode": proc.returncode, "stderr": proc.stderr.decode("utf-8", "replace")},
            )

        png_bytes = proc.stdout
        if not png_bytes or not png_bytes.startswith(b"\x89PNG"):
            raise BridgeError(
                "screencap returned non-PNG data",
                code="adroid.bridge.screenshot_invalid",
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
        serial = self._require_adb_serial(device_id)
        # Resolve the launcher activity for the package, then start it.
        raw = self._run_adb(
            ["-s", serial, "shell", "cmd", "package", "resolve-activity", "--brief", package_name]
        )
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        # Last non-empty line is the resolved component (pkg/.Activity) or a path.
        component = lines[-1] if lines else None
        if not component or "/" not in component:
            raise BridgeError(
                f"could not resolve launcher activity for {package_name}",
                code="adroid.bridge.no_launcher_activity",
                details={"package_name": package_name},
            )
        self._run_adb(["-s", serial, "shell", "am", "start", "-n", component])

    def force_stop_app(self, device_id: DeviceId, package_name: str) -> None:
        serial = self._require_adb_serial(device_id)
        self._run_adb(["-s", serial, "shell", "am", "force-stop", package_name])

    def tap(self, device_id: DeviceId, x: int, y: int) -> None:
        serial = self._require_adb_serial(device_id)
        self._run_adb(["-s", serial, "shell", "input", "tap", str(x), str(y)])

    def input_text(self, device_id: DeviceId, text: str) -> None:
        serial = self._require_adb_serial(device_id)
        # Escape spaces — adb shell splits on whitespace. v0.2.0 will use
        # `input text` with proper shell quoting via shlex.
        escaped = text.replace(" ", "%s")
        self._run_adb(["-s", serial, "shell", "input", "text", escaped])

    def press_key(self, device_id: DeviceId, keycode: str) -> None:
        serial = self._require_adb_serial(device_id)
        self._run_adb(["-s", serial, "shell", "input", "keyevent", keycode])

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _adb_cmd(self, args: list[str]) -> list[str]:
        cmd = [self._adb_path]
        if self._adb_host:
            cmd.extend(["-H", self._adb_host])
        return cmd + args

    def _run_adb(self, args: list[str]) -> str:
        cmd = self._adb_cmd(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise BridgeError(
                f"adb binary not found at {self._adb_path!r}",
                code="adroid.bridge.adb_missing",
                details={"adb_path": self._adb_path},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                f"adb command timed out: {' '.join(args)}",
                code="adroid.bridge.timeout",
            ) from exc

        if proc.returncode != 0:
            raise BridgeError(
                f"adb command failed: {' '.join(args)}",
                code="adroid.bridge.command_failed",
                details={
                    "returncode": proc.returncode,
                    "stderr": proc.stderr.decode("utf-8", "replace")[:2048],
                },
            )
        return proc.stdout.decode("utf-8", "replace")

    def _require_adb_serial(self, device_id: DeviceId) -> str:
        if device_id.kind != "adb":
            raise DeviceNotAvailableError(
                f"AdbBridge cannot target device of kind '{device_id.kind}'",
                code="adroid.bridge.kind_mismatch",
                details={"device_id": str(device_id)},
            )
        return device_id.value

    @staticmethod
    def _parse_devices_output(raw: str) -> list[DeviceInfo]:
        devices: list[DeviceInfo] = []
        # Sample:
        #   List of devices attached
        #   emulator-5554    device product:sdk_gphone64 model:sdk_gphone64 device:emu64x transport_id:1
        #   R5CR30XXXXX      unauthorized transport_id:2
        for line in raw.splitlines()[1:]:  # skip "List of devices attached"
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            serial = parts[0]
            state_token = parts[1] if len(parts) > 1 else "unknown"
            state = AdbBridge._parse_state(state_token)
            props = AdbBridge._parse_props(parts[2:])

            devices.append(
                DeviceInfo(
                    id=DeviceId(kind="adb", value=serial),
                    state=state,
                    model=props.get("model"),
                    manufacturer=props.get("device"),  # close enough for v0.1.0
                )
            )
        return devices

    @staticmethod
    def _parse_state(token: str) -> DeviceState:
        return {
            "device": DeviceState.ONLINE,
            "offline": DeviceState.OFFLINE,
            "unauthorized": DeviceState.UNAUTHORIZED,
            "bootloader": DeviceState.BOOTING,
        }.get(token, DeviceState.UNKNOWN)

    @staticmethod
    def _parse_props(tokens: list[str]) -> dict[str, str]:
        props: dict[str, str] = {}
        for tok in tokens:
            if ":" in tok:
                k, v = tok.split(":", 1)
                props[k] = v
        return props

    @staticmethod
    def _parse_packages_output(raw: str, *, system_flag: bool) -> list[AppInfo]:
        apps: list[AppInfo] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("package:"):
                continue
            pkg = line[len("package:"):].strip()
            if not pkg:
                continue
            apps.append(
                AppInfo(
                    package_name=pkg,
                    system_app=system_flag,
                )
            )
        return apps

    @staticmethod
    def _parse_png_dimensions(data: bytes) -> tuple[int, int]:
        # PNG IHDR: bytes 16-24 are width (4 bytes BE) and height (4 BE).
        if len(data) < 24:
            raise BridgeError(
                "PNG too short to extract dimensions",
                code="adroid.bridge.png_truncated",
            )
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return (width, height)


# ---------------------------------------------------------------------------
# Local filesystem BlobStore
# ---------------------------------------------------------------------------


class LocalBlobStore:
    """Content-addressed store backed by a local directory.

    Layout: ``<root>/<aa>/<bb>/<rest-of-sha256>`` where aa/bb are the first
    two byte pairs of the sha256. This shards files across 65k directories
    so we don't end up with millions of files in one dir.
    """

    def __init__(self, root_dir):
        from pathlib import Path
        self._root = Path(root_dir)
        self._root.mkdir(parents=True, exist_ok=True)

    def put(self, data: bytes) -> str:
        import os
        digest = hashlib.sha256(data).hexdigest()
        shard = self._root / digest[:2] / digest[2:4]
        shard.mkdir(parents=True, exist_ok=True)
        path = shard / digest
        if not path.exists():
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)  # atomic on POSIX
        return digest

    def get(self, blob_ref: str) -> bytes | None:
        path = self._root / blob_ref[:2] / blob_ref[2:4] / blob_ref
        if not path.exists():
            return None
        return path.read_bytes()

    def exists(self, blob_ref: str) -> bool:
        path = self._root / blob_ref[:2] / blob_ref[2:4] / blob_ref
        return path.exists()
