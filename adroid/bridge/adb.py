"""ADB-backed DeviceBridge implementation.

This is the only implementation that talks to a real device. It shells
out to the ``adb`` binary — we deliberately do NOT bundle the ADB Java
library, because:
    1. The adb binary is the most stable Android SDK surface (it has not
       had a breaking change in 10+ years).
    2. Shelling out isolates us from JVM version drift on integrator hosts.
    3. It is trivially mockable in tests by overriding ``_run_adb``.

Trade-off: subprocess overhead (~5ms per call on Linux). For v0.1.0
observation-heavy workloads this is fine.
ADB server connection for high-frequency polling.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from adroid.bridge.base import BlobStore
from adroid.bridge.uiautomator_parser import parse_uiautomator_xml
from adroid.contract.errors import BridgeError, DeviceNotAvailableError
from adroid.contract.types import AppInfo, DeviceId, DeviceInfo, DeviceState, Screenshot
from adroid.contract.ui import (
    SemanticNode,
    Selector,
    TapElementResult,
    WaitConditionType,
    WaitForCondition,
    WaitForResult,
    WorldState,
)


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
        # for v0.1.0;
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
        """Launch an app by package name.

        v0.2.0: MIUI-aware fallback chain. Tries 3 methods in order:
          1. cmd package resolve-activity → am start -n (stock Android)
          2. monkey -p <pkg> 1 (MIUI-friendly, works on most devices)
          3. am start -a MAIN -c LAUNCHER -n <pkg>/<pkg>.MainActivity (last resort)

        Raises BridgeError only if ALL three methods fail.
        """
        serial = self._require_adb_serial(device_id)

        # Method 1: resolve-activity + am start (stock Android)
        try:
            raw = self._run_adb(
                ["-s", serial, "shell", "cmd", "package", "resolve-activity", "--brief", package_name]
            )
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            # Last non-empty line is the resolved component (pkg/.Activity) or a path.
            component = lines[-1] if lines else None
            if component and "/" in component:
                self._run_adb(["-s", serial, "shell", "am", "start", "-n", component])
                return
        except BridgeError:
            pass  # fall through to method 2

        # Method 2: monkey -p (MIUI-friendly)
        try:
            self._run_adb([
                "-s", serial, "shell", "monkey", "-p", package_name, "1"
            ])
            return
        except BridgeError:
            pass  # fall through to method 3

        # Method 3: am start with MAIN/LAUNCHER intent (last resort)
        # Guess the activity name — usually <package>/<package>.MainActivity
        # This rarely works but is worth trying.
        try:
            guessed_activity = f"{package_name}/{package_name}.MainActivity"
            self._run_adb([
                "-s", serial, "shell", "am", "start",
                "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.LAUNCHER",
                "-n", guessed_activity,
            ])
            return
        except BridgeError as exc:
            raise BridgeError(
                f"could not launch {package_name}: all 3 methods failed (resolve-activity, monkey, am start)",
                code="adroid.bridge.launch_failed",
                details={"package_name": package_name},
            ) from exc

    def force_stop_app(self, device_id: DeviceId, package_name: str) -> None:
        serial = self._require_adb_serial(device_id)
        self._run_adb(["-s", serial, "shell", "am", "force-stop", package_name])

    def tap(self, device_id: DeviceId, x: int, y: int) -> None:
        serial = self._require_adb_serial(device_id)
        self._run_adb(["-s", serial, "shell", "input", "tap", str(x), str(y)])

    def input_text(self, device_id: DeviceId, text: str) -> None:
        serial = self._require_adb_serial(device_id)
        # Escape spaces — adb shell splits on whitespace.
        # `input text` with proper shell quoting via shlex.
        escaped = text.replace(" ", "%s")
        self._run_adb(["-s", serial, "shell", "input", "text", escaped])

    def press_key(self, device_id: DeviceId, keycode: str) -> None:
        serial = self._require_adb_serial(device_id)
        self._run_adb(["-s", serial, "shell", "input", "keyevent", keycode])

    def tap_text(
        self,
        device_id: DeviceId,
        text: str,
        *,
        match: str = "first",
        scroll_to_visible: bool = True,
    ) -> dict:
        """Tap an element by visible text via UIAutomator dump.

        Flow:
            1. `uiautomator dump` → produces XML with all visible elements
            2. Parse XML, find <node> elements with text= or content-desc= matching
            3. If match not found and scroll_to_visible=True, swipe down and retry (up to 5x)
            4. Tap the center of the matched element's bounds
        """
        import time

        serial = self._require_adb_serial(device_id)
        start = time.monotonic()
        scrolled = False
        attempts = 0
        max_scroll_attempts = 5 if scroll_to_visible else 1

        while attempts < max_scroll_attempts:
            # Dump UI hierarchy to /sdcard, then pull it
            dump_path = "/sdcard/adroid-ui-dump.xml"
            self._run_adb(["-s", serial, "shell", "uiautomator", "dump", dump_path])
            xml_raw = self._run_adb(["-s", serial, "shell", "cat", dump_path])
            # Cleanup
            try:
                self._run_adb(["-s", serial, "shell", "rm", dump_path])
            except BridgeError:
                pass  # cleanup failure is non-fatal

            # Parse and find matches
            matches = self._find_elements_by_text(xml_raw, text)
            if matches:
                # Pick the right match
                if match == "last":
                    target = matches[-1]
                elif match == "best":
                    target = max(matches, key=lambda e: len(e.get("text", "")))
                else:  # "first" (default)
                    target = matches[0]

                bounds = target["bounds"]
                cx = (bounds["x"] + bounds["w"] // 2) + bounds["x"]
                cy = (bounds["y"] + bounds["h"] // 2) + bounds["y"]
                # Actually bounds in uiautomator XML is [x1,y1][x2,y2]
                # We stored x,y,w,h — let's fix the center calc:
                cx = bounds["x"] + bounds["w"] // 2
                cy = bounds["y"] + bounds["h"] // 2

                # Tap it
                self.tap(device_id, cx, cy)

                duration_ms = int((time.monotonic() - start) * 1000)
                return {
                    "matched_elements": len(matches),
                    "tapped_element": {
                        "text": target["text"],
                        "bounds": bounds,
                    },
                    "scrolled": scrolled,
                    "duration_ms": duration_ms,
                }

            # Not found — try scrolling
            if scroll_to_visible and attempts < max_scroll_attempts - 1:
                # Swipe from middle-bottom to middle-top (scroll down)
                # Default to 1080x2400 if we don't know screen size
                self._run_adb([
                    "-s", serial, "shell", "input", "swipe",
                    "540", "1800", "540", "600", "300"
                ])
                scrolled = True
                time.sleep(0.5)  # let UI settle

            attempts += 1

        # No match found after all attempts
        raise BridgeError(
            f"no element found with text={text!r} after {attempts} attempt(s)",
            code="adroid.bridge.tap_text_not_found",
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
        serial = self._require_adb_serial(device_id)
        self._run_adb([
            "-s", serial, "shell", "input", "swipe",
            str(x1), str(y1), str(x2), str(y2), str(duration_ms),
        ])

    def run_shell(self, device_id: DeviceId, command: str) -> dict:
        """Execute an allowlisted shell command via adb shell.

        NOTE: The runtime layer must call AllowlistChecker.check() BEFORE
        invoking this method. This implementation does NOT re-check —
        defense in depth is the responsibility of the gate, not the bridge.
        """
        import time
        serial = self._require_adb_serial(device_id)
        start = time.monotonic()

        # Use shell=True=False with explicit args; adb shell passes the
        # rest as a single argument string to the device shell.
        import subprocess
        cmd = self._adb_cmd(["-s", serial, "shell", command])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(
                f"shell command timed out: {command}",
                code="adroid.bridge.shell_timeout",
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
        """Read recent logcat entries via `adb logcat -d`."""
        serial = self._require_adb_serial(device_id)
        # -d = dump and exit, -t N = last N lines
        args = ["-s", serial, "shell", "logcat", "-d", "-t", str(lines)]
        if filter:
            args.extend(filter.split())

        raw = self._run_adb(args)
        all_lines = raw.splitlines()
        # Cap at requested lines (logcat -t may return slightly more)
        result_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
        total_bytes = sum(len(l.encode("utf-8")) for l in result_lines)
        return {
            "lines": result_lines,
            "truncated": len(all_lines) > lines,
            "total_bytes": total_bytes,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _find_elements_by_text(xml_raw: str, text: str) -> list[dict]:
        """Parse uiautomator XML and find all elements whose text or
        content-desc matches the given text exactly (case-sensitive).

        Returns a list of dicts: {text, content_desc, bounds, class}
        """
        # uiautomator XML sometimes has encoding declaration that fails
        # to parse; strip it
        if "<?xml" in xml_raw:
            # Keep the XML declaration but force UTF-8 encoding
            end_decl = xml_raw.find("?>")
            if end_decl > 0:
                xml_raw = '<?xml version="1.0" encoding="UTF-8"?>' + xml_raw[end_decl + 2:]

        try:
            root = ET.fromstring(xml_raw)
        except ET.ParseError as exc:
            raise BridgeError(
                f"failed to parse uiautomator dump: {exc}",
                code="adroid.bridge.ui_dump_parse_error",
                details={"xml_snippet": xml_raw[:500]},
            ) from exc

        matches: list[dict] = []
        for node in root.iter("node"):
            node_text = node.get("text", "")
            node_desc = node.get("content-desc", "")
            if text == node_text or text == node_desc:
                bounds_str = node.get("bounds", "[0,0][0,0]")
                bounds = AdbBridge._parse_bounds(bounds_str)
                matches.append({
                    "text": node_text or node_desc,
                    "content_desc": node_desc,
                    "bounds": bounds,
                    "class": node.get("class", ""),
                })
        return matches

    @staticmethod
    def _parse_bounds(bounds_str: str) -> dict:
        """Parse uiautomator bounds string '[x1,y1][x2,y2]' into dict.

        Returns {x, y, w, h} where (x, y) is top-left corner.
        """
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str)
        if not m:
            return {"x": 0, "y": 0, "w": 0, "h": 0}
        x1, y1, x2, y2 = (int(g) for g in m.groups())
        return {"x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1}

    # ------------------------------------------------------------------
    # v0.2.0 Semantic UI tools
    # ------------------------------------------------------------------

    # Cache: device_serial → (WorldState, timestamp). TTL = 2 seconds.
    # Prevents redundant uiautomator dumps when agent does find+tap+wait
    # in rapid succession.
    _worldstate_cache: dict[str, tuple[WorldState, float]] = {}
    _worldstate_cache_ttl: float = 2.0
    _worldstate_revision_counter: int = 0

    def ui_dump(self, device_id: DeviceId) -> WorldState:
        """Dump UI hierarchy via `uiautomator dump` → parse to WorldState."""
        serial = self._require_adb_serial(device_id)

        # 1. Run uiautomator dump to /sdcard, then cat it back
        dump_path = "/sdcard/adroid-ui-dump.xml"
        self._run_adb(["-s", serial, "shell", "uiautomator", "dump", dump_path])
        xml_raw = self._run_adb(["-s", serial, "shell", "cat", dump_path])
        # Cleanup (non-fatal if fails)
        try:
            self._run_adb(["-s", serial, "shell", "rm", dump_path])
        except BridgeError:
            pass

        # 2. Parse XML into SemanticNodes
        nodes = parse_uiautomator_xml(xml_raw)

        # 3. Detect foreground package + activity via dumpsys
        fg_pkg = self._detect_foreground_package(serial)
        activity = self._detect_current_activity(serial)

        # 4. Detect keyboard visibility
        keyboard_visible = self._detect_keyboard_visible(serial)

        # 5. Detect screen dimensions from wm size
        screen_dims = self._detect_screen_dimensions(serial)

        # 6. Store raw XML in blob store for debugging
        raw_xml_ref = self._blob_store.put(xml_raw.encode("utf-8")) if xml_raw else None

        # 7. Build WorldState
        AdbBridge._worldstate_revision_counter += 1
        ws = WorldState(
            revision=AdbBridge._worldstate_revision_counter,
            timestamp=datetime.now(timezone.utc),
            foreground_package=fg_pkg,
            activity=activity,
            nodes=tuple(nodes),
            keyboard_visible=keyboard_visible,
            screen_dimensions=screen_dims,
            raw_xml_ref=raw_xml_ref,
        )

        # Cache it
        AdbBridge._worldstate_cache[serial] = (ws, time.monotonic())

        return ws

    def _get_cached_worldstate(self, serial: str, max_age: float | None = None) -> WorldState | None:
        """Return cached WorldState if fresh enough, else None."""
        ttl = max_age if max_age is not None else self._worldstate_cache_ttl
        cached = AdbBridge._worldstate_cache.get(serial)
        if cached is None:
            return None
        ws, ts = cached
        if time.monotonic() - ts > ttl:
            return None
        return ws

    def ui_find_elements(
        self,
        device_id: DeviceId,
        selector: Selector,
    ) -> tuple[SemanticNode, ...]:
        """Find nodes matching selector. Uses cache if fresh."""
        serial = self._require_adb_serial(device_id)
        ws = self._get_cached_worldstate(serial)
        if ws is None:
            ws = self.ui_dump(device_id)
        return ws.find(selector)

    def ui_tap_element(
        self,
        device_id: DeviceId,
        selector: Selector,
    ) -> TapElementResult:
        """Find node by selector, tap center of its bounds."""
        start = time.monotonic()
        matches = self.ui_find_elements(device_id, selector)

        if not matches:
            return TapElementResult(
                matched_nodes=0,
                tapped=False,
                selector_strategy="a11y",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

        # Pick per match strategy (find() already does this, but be explicit)
        if selector.match == "last":
            target = matches[-1]
        elif selector.match == "best":
            target = max(matches, key=lambda n: len(n.text or n.description or ""))
        else:
            target = matches[0]

        cx, cy = target.bounds.center
        self.tap(device_id, cx, cy)

        return TapElementResult(
            matched_nodes=len(matches),
            tapped=True,
            tapped_node=target,
            tap_coordinates={"x": cx, "y": cy},
            selector_strategy="a11y",
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def ui_wait_for(
        self,
        device_id: DeviceId,
        condition: WaitForCondition,
    ) -> WaitForResult:
        """Poll device until condition met or timeout."""
        self._require_adb_serial(device_id)  # validate device
        start = time.monotonic()
        polls = 0
        last_revision: int | None = None
        last_matched_uuid: str | None = None
        stable_count = 0
        prev_node_hashes: set[str] | None = None

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= condition.timeout_seconds:
                return WaitForResult(
                    status="timeout",
                    condition_type=condition.type,
                    duration_seconds=elapsed,
                    polls=polls,
                    final_state_revision=last_revision,
                    matched_node_uuid=last_matched_uuid,
                )

            # Force fresh dump (bypass cache) — wait_for needs real-time state
            ws = self.ui_dump(device_id)
            polls += 1
            last_revision = ws.revision

            # Check condition
            satisfied = False

            if condition.type == WaitConditionType.UI_STABLE:
                # v0.3.6: Use Rust hash for speed (0.45ms vs Python's ~5ms for 1000 nodes)
                try:
                    import adroid_rust
                    ws_json = json.dumps(ws.model_dump(mode="json"))
                    current_hash = adroid_rust.compute_state_hash(ws_json)
                except ImportError:
                    # Python fallback: hash node texts+bounds+ids
                    current_hash = str(hash(tuple(
                        (n.text, n.description, n.resource_id, n.bounds.x, n.bounds.y,
                         n.bounds.w, n.bounds.h, n.clickable, n.focused)
                        for n in ws.nodes
                    )))
                if prev_node_hashes is None:
                    prev_node_hashes = {current_hash}
                    stable_count = 1
                elif current_hash in prev_node_hashes:
                    stable_count += 1
                else:
                    prev_node_hashes = {current_hash}
                    stable_count = 1

                if stable_count >= condition.stable_count:
                    satisfied = True

            elif condition.type == WaitConditionType.ELEMENT_APPEARS:
                if condition.selector:
                    matches = ws.find(condition.selector)
                    if matches:
                        satisfied = True
                        last_matched_uuid = matches[0].uuid

            elif condition.type == WaitConditionType.ELEMENT_DISAPPEARS:
                if condition.selector:
                    matches = ws.find(condition.selector)
                    if not matches:
                        satisfied = True

            elif condition.type == WaitConditionType.TEXT_VISIBLE:
                if condition.text:
                    if any(n.text == condition.text for n in ws.nodes):
                        satisfied = True
                        last_matched_uuid = next(
                            (n.uuid for n in ws.nodes if n.text == condition.text), None
                        )

            elif condition.type == WaitConditionType.TEXT_DISAPPEARS:
                if condition.text:
                    if not any(n.text == condition.text for n in ws.nodes):
                        satisfied = True

            elif condition.type == WaitConditionType.KEYBOARD_VISIBLE:
                if ws.keyboard_visible:
                    satisfied = True

            elif condition.type == WaitConditionType.KEYBOARD_HIDDEN:
                if not ws.keyboard_visible:
                    satisfied = True

            if satisfied:
                return WaitForResult(
                    status="satisfied",
                    condition_type=condition.type,
                    duration_seconds=time.monotonic() - start,
                    polls=polls,
                    final_state_revision=last_revision,
                    matched_node_uuid=last_matched_uuid,
                )

            # Sleep before next poll
            time.sleep(condition.poll_interval_seconds)

    # ------------------------------------------------------------------
    # Detection helpers for ui_dump
    # ------------------------------------------------------------------

    def _detect_foreground_package(self, serial: str) -> str | None:
        """Detect current foreground app package via dumpsys activity."""
        try:
            raw = self._run_adb([
                "-s", serial, "shell", "dumpsys", "activity", "activities"
            ])
            # Look for "mResumedActivity" or "topResumedActivity" line
            for line in raw.splitlines():
                line = line.strip()
                if "mResumedActivity" in line or "topResumedActivity" in line:
                    # Format: mResumedActivity=ActivityRecord{... u0 com.example.app/.MainActivity ...}
                    parts = line.split()
                    for part in parts:
                        if "/" in part and "." in part:
                            # com.example.app/.MainActivity
                            pkg = part.split("/")[0]
                            return pkg
            return None
        except BridgeError:
            return None

    def _detect_current_activity(self, serial: str) -> str | None:
        """Detect current activity name via dumpsys activity."""
        try:
            raw = self._run_adb([
                "-s", serial, "shell", "dumpsys", "activity", "activities"
            ])
            for line in raw.splitlines():
                line = line.strip()
                if "mResumedActivity" in line or "topResumedActivity" in line:
                    parts = line.split()
                    for part in parts:
                        if "/" in part and "." in part:
                            # com.example.app/.MainActivity → return .MainActivity
                            slash_idx = part.find("/")
                            if slash_idx >= 0:
                                return part[slash_idx + 1:]
            return None
        except BridgeError:
            return None

    def _detect_keyboard_visible(self, serial: str) -> bool:
        """Detect if soft keyboard is visible via dumpsys input_method."""
        try:
            raw = self._run_adb([
                "-s", serial, "shell", "dumpsys", "input_method"
            ])
            # Look for mInputShown=true (verified working on Android 11+)
            return "mInputShown=true" in raw
        except BridgeError:
            return False

    def _detect_screen_dimensions(self, serial: str) -> dict[str, int]:
        """Detect screen dimensions via wm size."""
        try:
            raw = self._run_adb(["-s", serial, "shell", "wm", "size"])
            # Format: "Physical size: 1080x2340"
            for line in raw.splitlines():
                line = line.strip()
                if "Physical size:" in line:
                    size_str = line.split(":", 1)[1].strip()
                    if "x" in size_str:
                        w, h = size_str.split("x", 1)
                        return {"width": int(w), "height": int(h)}
            return {"width": 0, "height": 0}
        except (BridgeError, ValueError):
            return {"width": 0, "height": 0}

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
