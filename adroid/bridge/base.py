"""Device bridge — abstract Protocol that every driver must implement.

The Protocol is what makes Adroid a *runtime* and not a *wrapper*. The
runtime only ever talks to ``DeviceBridge``; the actual driver (ADB,
UIAutomator, accessibility service, Termux helper, future drivers that
don't exist yet) is a swappable detail.

v0.1.0 ships only ``adb`` and ``mock`` implementations. v0.2.0 will add
``emulator`` (qemu control surface) and v0.3.0 will add ``remote``
(grpc-based for cross-network bridges).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from adroid.contract.types import AppInfo, DeviceId, DeviceInfo, Screenshot


@runtime_checkable
class DeviceBridge(Protocol):
    """Structural interface every driver must satisfy.

    Implementations may be sync or async — v0.1.0 is sync-only. The runtime
    wraps blocking calls in a thread pool when it eventually goes async.

    Every method must:
        - Return a frozen value object (never a dict or a tuple).
        - Raise ``BridgeError`` on driver failure, never raw ``Exception``.
        - Never leak driver-specific identifiers (e.g. ADB shell error
          strings) into the return value. Wrap them in ``BridgeError.details``.
    """

    def list_devices(self) -> list[DeviceInfo]:
        """Return a snapshot of every device the bridge can see."""
        ...

    def get_device_info(self, device_id: DeviceId) -> DeviceInfo:
        """Return a fresh snapshot of one device."""
        ...

    def list_installed_apps(self, device_id: DeviceId) -> list[AppInfo]:
        """List installed packages on the device."""
        ...

    def capture_screenshot(self, device_id: DeviceId) -> Screenshot:
        """Capture a screenshot. Returns a Screenshot with blob_ref
        (sha256). Bytes are stored via the runtime's BlobStore, NOT here."""
        ...

    def launch_app(self, device_id: DeviceId, package_name: str) -> None:
        """Launch an app by package name."""
        ...

    def force_stop_app(self, device_id: DeviceId, package_name: str) -> None:
        """Force-stop an app by package name."""
        ...

    def tap(self, device_id: DeviceId, x: int, y: int) -> None:
        """Inject a tap event at (x, y). Coordinates are device pixels."""
        ...

    def input_text(self, device_id: DeviceId, text: str) -> None:
        """Inject text input. Unicode-safe."""
        ...

    def press_key(self, device_id: DeviceId, keycode: str) -> None:
        """Press an Android keycode (e.g. 'KEYCODE_HOME')."""
        ...


class BlobStore(Protocol):
    """Content-addressed store for binary artifacts (screenshots, etc.).

    v0.1.0 ships a local-filesystem implementation. v0.2.0 will add an
    S3-backed implementation. Both implement this Protocol.
    """

    def put(self, data: bytes) -> str:
        """Store ``data``. Returns its sha256 hex digest (the blob_ref)."""
        ...

    def get(self, blob_ref: str) -> bytes | None:
        """Return bytes for ``blob_ref`` or None if missing."""
        ...

    def exists(self, blob_ref: str) -> bool: ...
