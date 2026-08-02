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

    def input_text(self, device_id: DeviceId, text: str) -> None:
        self._require(device_id)

    def press_key(self, device_id: DeviceId, keycode: str) -> None:
        self._require(device_id)

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
