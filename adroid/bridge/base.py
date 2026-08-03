"""Device bridge — abstract Protocol that every driver must implement.

The Protocol is what makes Adroid a *runtime* and not a *wrapper*. The
runtime only ever talks to ``DeviceBridge``; the actual driver (ADB,
UIAutomator, accessibility service, Termux helper, future drivers that
don't exist yet) is a swappable detail.

v0.1.0 ships ``adb``, ``mock``, and ``termux`` implementations. v0.2.0
will add ``emulator`` (qemu control surface) and 
``remote`` (grpc-based for cross-network bridges).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from adroid.contract.types import AppInfo, DeviceId, DeviceInfo, Screenshot

if TYPE_CHECKING:
    from adroid.contract.ui import (
        SemanticNode,
        Selector,
        TapElementResult,
        WaitForCondition,
        WaitForResult,
        WorldState,
    )


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

    # ------------------------------------------------------------------
    # v0.2.0 Semantic UI tools
    # ------------------------------------------------------------------

    def ui_dump(self, device_id: DeviceId) -> "WorldState":
        """Dump the current UI hierarchy as a structured WorldState.

        Implementation should:
            1. Run `uiautomator dump` on the device
            2. Parse the XML into SemanticNode objects
            3. Detect foreground package + activity
            4. Detect keyboard visibility
            5. Detect screen dimensions
            6. Return a frozen WorldState

        The runtime caches the latest WorldState per device so that
        repeated ui_dump calls within a short window return cached
        state (avoiding redundant 1-2s uiautomator calls).

        Returns:
            WorldState — frozen snapshot of device UI.
        """
        ...

    def ui_find_elements(
        self,
        device_id: DeviceId,
        selector: "Selector",
    ) -> tuple["SemanticNode", ...]:
        """Query the latest WorldState for nodes matching the selector.

        This is a pure query — no device interaction. It uses the cached
        WorldState from the last ui_dump (or triggers a fresh dump if
        none exists). The runtime enforces a maximum age (default 2s)
        before forcing a re-dump.

        Returns:
            Tuple of matching SemanticNodes (empty if none).
        """
        ...

    def ui_tap_element(
        self,
        device_id: DeviceId,
        selector: "Selector",
    ) -> "TapElementResult":
        """Find a node matching the selector and tap its center.

        Implementation flow:
            1. ui_find_elements(selector) → get matches
            2. Pick match per selector.match strategy (first/last/best)
            3. Compute center of bounds
            4. Call self.tap(device_id, cx, cy)
            5. Return TapElementResult with the tapped node

        If no match found, returns TapElementResult(matched_nodes=0,
        tapped=False) — does NOT raise. Agents decide how to handle
        (retry with different selector, give up, ask user).
        """
        ...

    def ui_wait_for(
        self,
        device_id: DeviceId,
        condition: "WaitForCondition",
    ) -> "WaitForResult":
        """Poll the device until a condition is met or timeout.

        Implementation flow:
            1. Loop until timeout_seconds elapsed:
                a. ui_dump → fresh WorldState
                b. Check condition against WorldState
                c. If satisfied → return WaitForResult(status="satisfied")
                d. Sleep poll_interval_seconds
            2. If timeout → return WaitForResult(status="timeout")

        Conditions:
            ui_stable          — needs stable_count consecutive identical dumps
            element_appears    — selector.find() returns non-empty
            element_disappears — selector.find() returns empty
            text_visible       — any node.text == condition.text
            text_disappears    — no node.text == condition.text
            keyboard_visible   — WorldState.keyboard_visible == True
            keyboard_hidden    — WorldState.keyboard_visible == False

        Does NOT raise on timeout — returns WaitForResult(status="timeout")
        so agents can branch on the result without try/except.
        """
        ...


class BlobStore(Protocol):
    """Content-addressed store for binary artifacts (screenshots, etc.).

    v0.1.0 ships a local-filesystem implementation.
    S3-backed implementation. Both implement this Protocol.
    """

    def put(self, data: bytes) -> str:
        """Store ``data``. Returns its sha256 hex digest (the blob_ref)."""
        ...

    def get(self, blob_ref: str) -> bytes | None:
        """Return bytes for ``blob_ref`` or None if missing."""
        ...

    def exists(self, blob_ref: str) -> bool: ...
