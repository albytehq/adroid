"""Device bridge — abstract Protocol that every driver must implement.

The Protocol is what makes Adroid a *runtime* and not a *wrapper*. The
runtime only ever talks to ``DeviceBridge``; the actual driver (ADB,
UIAutomator, accessibility service, Termux helper, future drivers that
don't exist yet) is a swappable detail.

v0.1.0 ships ``adb``, ``mock``, and ``termux`` implementations. v0.2.0
will add ``emulator`` (qemu control surface) and v0.3.0 will add
``remote`` (grpc-based for cross-network bridges).
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

    # ------------------------------------------------------------------
    # Device observation (READ scope)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # App lifecycle (TOUCH_CONTROL scope)
    # ------------------------------------------------------------------

    def launch_app(self, device_id: DeviceId, package_name: str) -> None:
        """Launch an app by package name."""
        ...

    def force_stop_app(self, device_id: DeviceId, package_name: str) -> None:
        """Force-stop an app by package name."""
        ...

    # ------------------------------------------------------------------
    # Input injection (TOUCH_CONTROL / KEYBOARD_INPUT scope)
    # ------------------------------------------------------------------

    def tap(self, device_id: DeviceId, x: int, y: int) -> None:
        """Inject a tap event at (x, y). Coordinates are device pixels."""
        ...

    def tap_text(
        self,
        device_id: DeviceId,
        text: str,
        *,
        match: str = "first",
        scroll_to_visible: bool = True,
    ) -> dict:
        """Tap an element by visible text (semantic tap).

        Dumps the UI tree, finds an element whose text/content-desc matches,
        optionally scrolls to make it visible, then taps the center of its
        bounding box.

        Args:
            text: The text to match (case-sensitive exact match).
            match: "first" (default), "last", or "best" (longest match).
            scroll_to_visible: If True, scroll down until element is found
                or 5 scroll attempts fail.

        Returns:
            Dict with keys: matched_elements, tapped_element {text, bounds},
            scrolled (bool), duration_ms.
        """
        ...

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
        """Swipe gesture from (x1, y1) to (x2, y2) over duration_ms."""
        ...

    def input_text(self, device_id: DeviceId, text: str) -> None:
        """Inject text input. Unicode-safe."""
        ...

    def press_key(self, device_id: DeviceId, keycode: str) -> None:
        """Press an Android keycode (e.g. 'KEYCODE_HOME')."""
        ...

    # ------------------------------------------------------------------
    # Shell execution (SHELL_EXEC scope — allowlisted)
    # ------------------------------------------------------------------

    def run_shell(self, device_id: DeviceId, command: str) -> dict:
        """Execute an allowlisted shell command.

        The runtime's allowlist module must approve the command before
        this is called. Implementations should still log the command
        verbatim into the audit trail.

        Returns:
            Dict with: stdout (str), stderr (str), returncode (int),
            duration_ms (int).
        """
        ...

    # ------------------------------------------------------------------
    # Log observation (NOTIFICATIONS_READ scope)
    # ------------------------------------------------------------------

    def read_logs(
        self,
        device_id: DeviceId,
        *,
        lines: int = 100,
        filter: str | None = None,
    ) -> dict:
        """Read recent logcat entries.

        Args:
            lines: Number of recent lines to return (default 100).
            filter: Optional tag filter (e.g. "ActivityManager:I *:S").

        Returns:
            Dict with: lines (list[str]), truncated (bool), total_bytes (int).
        """
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
