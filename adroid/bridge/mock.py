"""Mock DeviceBridge for tests and local development without a device.

Deterministic, in-process, no I/O. Returns canned data so the runtime
layer can be exercised end-to-end without a real Android device.

The mock also doubles as a reference implementation — every other bridge
should produce identical value-object shapes for the same logical state.
"""

from __future__ import annotations

import hashlib
import threading
from collections import defaultdict
from dataclasses import dataclass, field

from adroid.bridge.base import BlobStore
from adroid.contract.errors import DeviceNotAvailableError
from adroid.contract.types import AppInfo, DeviceId, DeviceInfo, DeviceState, Screenshot


@dataclass
class _MockDevice:
    info: DeviceInfo
    apps: list[AppInfo]
    last_screenshot: bytes | None = None
    last_tap: tuple[int, int] | None = None
    last_swipe: tuple | None = None
    last_text: str | None = None
    last_key: str | None = None


class MockBridge:
    """In-memory bridge. Thread-safe. Deterministic outputs."""

    def __init__(self, *, blob_store: BlobStore):
        self._blob_store = blob_store
        self._devices: dict[str, _MockDevice] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Test helpers (NOT part of the DeviceBridge Protocol)
    # ------------------------------------------------------------------

    def add_device(self, info: DeviceInfo, *, apps: list[AppInfo] | None = None) -> None:
        with self._lock:
            self._devices[str(info.id)] = _MockDevice(
                info=info,
                apps=apps or [AppInfo(package_name="com.android.settings", system_app=True)],
            )

    def remove_device(self, device_id: DeviceId) -> None:
        with self._lock:
            self._devices.pop(str(device_id), None)

    # ------------------------------------------------------------------
    # DeviceBridge Protocol
    # ------------------------------------------------------------------

    def list_devices(self) -> list[DeviceInfo]:
        with self._lock:
            return [d.info for d in self._devices.values()]

    def get_device_info(self, device_id: DeviceId) -> DeviceInfo:
        with self._lock:
            d = self._devices.get(str(device_id))
            if d is None:
                raise DeviceNotAvailableError(
                    f"mock device not found: {device_id}",
                    code="adroid.bridge.mock_device_missing",
                    details={"device_id": str(device_id)},
                )
            return d.info

    def list_installed_apps(self, device_id: DeviceId) -> list[AppInfo]:
        self._require(device_id)
        with self._lock:
            return list(self._devices[str(device_id)].apps)

    def capture_screenshot(self, device_id: DeviceId) -> Screenshot:
        info = self._require(device_id)
        # Generate a deterministic 1x1 PNG (red pixel) so tests have stable
        # hashes. Real screenshots will vary — this is just a mock.
        png = (
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
            b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01\x8d\xa5K-\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        blob_ref = self._blob_store.put(png)
        with self._lock:
            self._devices[str(device_id)].last_screenshot = png
        return Screenshot(
            device_id=device_id,
            blob_ref=blob_ref,
            width=1,
            height=1,
        )

    def launch_app(self, device_id: DeviceId, package_name: str) -> None:
        self._require(device_id)
        # No-op in mock; real impl would resolve + am start.

    def force_stop_app(self, device_id: DeviceId, package_name: str) -> None:
        self._require(device_id)

    def tap(self, device_id: DeviceId, x: int, y: int) -> None:
        self._require(device_id)
        # Record the tap in mock state (for testing)
        with self._lock:
            d = self._devices[str(device_id)]
            d.last_tap = (x, y)

    def tap_text(
        self,
        device_id: DeviceId,
        text: str,
        *,
        match: str = "first",
        scroll_to_visible: bool = True,
    ) -> dict:
        """Mock tap_text — always succeeds with a synthetic element match.

        Useful for testing the runtime/gate/web layers without a real device.
        Returns canned bounds 100,200 - 400,250 (300x50 button).
        """
        self._require(device_id)
        bounds = {"x": 100, "y": 200, "w": 300, "h": 50}
        cx = bounds["x"] + bounds["w"] // 2  # 250
        cy = bounds["y"] + bounds["h"] // 2  # 225
        with self._lock:
            d = self._devices[str(device_id)]
            d.last_tap = (cx, cy)
        return {
            "matched_elements": 1,
            "tapped_element": {"text": text, "bounds": bounds},
            "scrolled": False,
            "duration_ms": 5,
        }

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
        self._require(device_id)
        with self._lock:
            d = self._devices[str(device_id)]
            d.last_swipe = ((x1, y1), (x2, y2), duration_ms)

    def input_text(self, device_id: DeviceId, text: str) -> None:
        self._require(device_id)
        with self._lock:
            d = self._devices[str(device_id)]
            d.last_text = text

    def press_key(self, device_id: DeviceId, keycode: str) -> None:
        self._require(device_id)
        with self._lock:
            d = self._devices[str(device_id)]
            d.last_key = keycode

    def run_shell(self, device_id: DeviceId, command: str) -> dict:
        """Mock run_shell — returns canned output for known commands,
        empty output for unknown ones. Always succeeds (returncode 0)."""
        self._require(device_id)
        # Provide realistic mock output for common commands
        canned_outputs = {
            "pm list packages -3": "package:com.example.app1\npackage:com.example.app2\n",
            "pm list packages": "package:com.android.settings\npackage:com.example.app1\n",
            "getprop ro.product.model": "MockPhone\n",
            "getprop ro.build.version.release": "14\n",
            "df -h": (
                "Filesystem      Size  Used Avail Use% Mounted on\n"
                "/dev/block/dm-0  128G   64G   64G  50% /\n"
            ),
            "uptime": " 12:34:56 up 1 day,  3:45,  0 users,  load average: 0.45, 0.32, 0.28\n",
            "ip addr": "1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN\n",
        }
        stdout = canned_outputs.get(command.strip(), "")
        return {
            "stdout": stdout,
            "stderr": "",
            "returncode": 0,
            "duration_ms": 12,
        }

    def read_logs(
        self,
        device_id: DeviceId,
        *,
        lines: int = 100,
        filter: str | None = None,
    ) -> dict:
        """Mock read_logs — returns canned logcat lines."""
        self._require(device_id)
        # Generate N fake logcat lines
        fake_lines = [
            f"08-02 12:34:{i:02d}.123  1234 5678 I ActivityManager: mock log line {i}"
            for i in range(min(lines, 20))
        ]
        return {
            "lines": fake_lines,
            "truncated": False,
            "total_bytes": sum(len(l.encode("utf-8")) for l in fake_lines),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require(self, device_id: DeviceId) -> DeviceInfo:
        with self._lock:
            d = self._devices.get(str(device_id))
            if d is None:
                raise DeviceNotAvailableError(
                    f"mock device not found: {device_id}",
                    code="adroid.bridge.mock_device_missing",
                    details={"device_id": str(device_id)},
                )
            return d.info


class InMemoryBlobStore:
    """Pure-Python BlobStore for tests. Not for production."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._blobs.setdefault(digest, data)
        return digest

    def get(self, blob_ref: str) -> bytes | None:
        with self._lock:
            return self._blobs.get(blob_ref)

    def exists(self, blob_ref: str) -> bool:
        with self._lock:
            return blob_ref in self._blobs
