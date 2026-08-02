"""v0.2.0 Semantic UI primitives — formal types for device state perception.

This module defines the WorldState, SemanticNode, Selector, and
WaitForCondition types that power the semantic UI layer. These are the
foundation for the AI Device Kernel (ADK) proposal — but deliberately
minimal for v0.2.0. No planner, no IR, no bytecode. Just structured
state that AI agents can reason about without pixel coordinates.

Design decisions (research-based):

1. WorldState is a snapshot, NOT a stream.
   uiautomator dump takes 1-2 seconds per call (verified via Appium docs
   and duoplus 2025 guide). Real-time event stream requires Accessibility
   Service APK (deferred to v0.4.0 hybrid execution). For v0.2.0, polling
   is the correct tradeoff.

2. SemanticNode uses simple flat fields, not nested XML.
   XML parsing happens ONCE in the bridge layer. AI agents receive clean
   JSON. This makes the WorldState trivially queryable with Selector.

3. Selector uses exact-match semantics with explicit "contains" variants.
   Substring matching is opt-in via text_contains. This avoids the common
   bug where "Search" matches "SearchSettings" unintentionally.

4. WaitForCondition is polling-based, not event-based.
   Conditions: ui_stable, element_appears, element_disappears,
   text_visible, keyboard_visible. The runtime polls ui.dump at
   poll_interval_seconds until condition met or timeout.

5. confidence field is reserved for v0.3.0+ when we add OCR/vision.
   For v0.2.0, all nodes come from uiautomator (a11y source), so
   confidence is always 1.0. The field exists now to avoid schema
   migration later.

6. source field tracks provenance ("a11y" | "ocr" | "vision").
   Same reasoning as confidence — future-proofing without current use.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Frozen base (consistent with rest of contract layer)
# ---------------------------------------------------------------------------


class _UIFrozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Bounds — top-left corner + dimensions
# ---------------------------------------------------------------------------


class Bounds(_UIFrozen):
    """Pixel-coordinate bounding box. (x, y) is top-left corner."""

    x: int = Field(ge=0)
    y: int = Field(ge=0)
    w: int = Field(ge=0)
    h: int = Field(ge=0)

    @property
    def center(self) -> tuple[int, int]:
        """Center pixel of the bounding box, rounded to nearest int."""
        return (self.x + self.w // 2, self.y + self.h // 2)

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    def contains(self, px: int, py: int) -> bool:
        """Does this bounding box contain pixel (px, py)?"""
        return self.x <= px < self.x2 and self.y <= py < self.y2


# ---------------------------------------------------------------------------
# SemanticNode — single UI element in the WorldState
# ---------------------------------------------------------------------------


class NodeRole(str, Enum):
    """Closed vocabulary of UI element roles.

    Derived from common Android widget classes. The `unknown` catch-all
    is for elements that don't fit any known role — AI agents should
    treat these with caution.
    """

    BUTTON = "button"
    TEXT = "text"
    TEXT_VIEW = "text_view"
    EDIT_TEXT = "edit_text"
    IMAGE = "image"
    IMAGE_VIEW = "image_view"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    SWITCH = "switch"
    TOGGLE = "toggle"
    SPINNER = "spinner"
    WEB_VIEW = "web_view"
    LIST = "list"
    LIST_ITEM = "list_item"
    SCROLL_VIEW = "scroll_view"
    RECYCLER_VIEW = "recycler_view"
    VIEW_PAGER = "view_pager"
    FRAME_LAYOUT = "frame_layout"
    LINEAR_LAYOUT = "linear_layout"
    RELATIVE_LAYOUT = "relative_layout"
    MENU_ITEM = "menu_item"
    TAB = "tab"
    DIALOG = "dialog"
    PROGRESS_BAR = "progress_bar"
    SEEK_BAR = "seek_bar"
    UNKNOWN = "unknown"


class SemanticNode(_UIFrozen):
    """Single UI element in the device's accessibility tree.

    All fields come directly from uiautomator dump XML attributes:
        text          → node@text
        description   → node@content-desc
        resource_id   → node@resource-id
        class_name    → node@class
        package       → node@package
        bounds        → parsed from node@bounds "[x1,y1][x2,y2]"
        clickable     → node@clickable == "true"
        scrollable    → node@scrollable == "true"
        editable      → node@focusable == "true" AND class contains "EditText"
                        (uiautomator has no direct "editable" attr)
        enabled       → node@enabled == "true"
        visible       → NOT (node@clickable == "false" AND ... visibility check)
                        For v0.2.0, we treat all dumped nodes as visible
                        (uiautomator dumps only visible windows by default).
        focused       → node@focused == "true"
        selected      → node@selected == "true"

    parent_uuid / children_uuids build the tree structure. AI agents can
    traverse parent/children for context-aware reasoning (e.g. "find the
    button INSIDE the dialog").

    confidence and source are reserved for v0.3.0+ when we add OCR/vision
    fallback. For v0.2.0 they're always 1.0 / "a11y".
    """

    uuid: str = Field(default_factory=lambda: uuid4().hex)
    role: NodeRole = NodeRole.UNKNOWN
    text: str | None = None
    description: str | None = None
    resource_id: str | None = None
    class_name: str | None = None
    package: str | None = None
    bounds: Bounds
    clickable: bool = False
    scrollable: bool = False
    editable: bool = False
    enabled: bool = True
    visible: bool = True
    focused: bool = False
    selected: bool = False
    parent_uuid: str | None = None
    children_uuids: tuple[str, ...] = Field(default_factory=tuple)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "a11y"


# ---------------------------------------------------------------------------
# WorldState — full snapshot of device UI
# ---------------------------------------------------------------------------


class WorldState(_UIFrozen):
    """Frozen snapshot of the device's UI state at a single point in time.

    revision is a monotonic counter per runtime. Two WorldStates with
    the same revision are guaranteed identical (frozen models).

    raw_xml_ref is a sha256 blob_ref to the original uiautomator XML dump.
    Stored for debugging — agents shouldn't need to read it, but the
    audit log + dashboard can use it for forensic review.

    keyboard_visible is detected via `dumpsys input_method | grep mInputShown`
    (verified working on Android 11+). Some MIUI builds may not report
    this accurately — agents should treat it as best-effort.
    """

    revision: int = Field(ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    foreground_package: str | None = None
    activity: str | None = None
    nodes: tuple[SemanticNode, ...] = Field(default_factory=tuple)
    keyboard_visible: bool = False
    screen_dimensions: dict[str, int] = Field(default_factory=lambda: {"width": 0, "height": 0})
    raw_xml_ref: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware (UTC)")
        return v.astimezone(timezone.utc)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def clickable_nodes(self) -> tuple[SemanticNode, ...]:
        return tuple(n for n in self.nodes if n.clickable and n.enabled and n.visible)

    def find(self, selector: "Selector") -> tuple[SemanticNode, ...]:
        """Query nodes by Selector. Returns matching nodes in tree order.

        match strategy (selector.match):
            "first"  → return first match (or empty tuple)
            "last"   → return last match
            "best"   → return longest-text match
            "all"    → return all matches
        """
        matches: list[SemanticNode] = []
        for node in self.nodes:
            if not _node_matches_selector(node, selector):
                continue
            matches.append(node)

        if not matches:
            return ()
        if selector.match == "all":
            return tuple(matches)
        if selector.match == "last":
            return (matches[-1],)
        if selector.match == "best":
            # "best" = longest text (most specific match)
            best = max(matches, key=lambda n: len(n.text or n.description or ""))
            return (best,)
        # default: "first"
        return (matches[0],)


# ---------------------------------------------------------------------------
# Selector — query specification for SemanticNode matching
# ---------------------------------------------------------------------------


class Selector(_UIFrozen):
    """Query specification for finding SemanticNodes in a WorldState.

    All fields are optional. A field that is None means "any value matches".
    Multiple non-None fields are AND-ed together (all must match).

    text vs text_contains:
        text is exact match (case-sensitive).
        text_contains is substring match (case-sensitive).
        If both are set, exact match wins (text_contains ignored).

    resource_id:
        uiautomator resource-ids are often "com.example.app:id/button1".
        For convenience, Selector.resource_id matches if:
          - exact match, OR
          - the node's resource_id ends with "/<selector.resource_id>"
        This lets agents write "button1" instead of the full
        "com.example.app:id/button1".

    class_name:
        Exact match. Use full class name like "android.widget.Button".
        For substring class matching, agents should use shell.exec +
        their own filtering — v0.2.0 deliberately keeps Selector simple.

    clickable/scrollable/editable:
        None = any. True/False = exact match.

    match:
        "first" (default) — return first match in tree order
        "last"             — return last match
        "best"             — return match with longest text (most specific)
        "all"              — return all matches
    """

    text: str | None = None
    text_contains: str | None = None
    resource_id: str | None = None
    description: str | None = None
    class_name: str | None = None
    clickable: bool | None = None
    scrollable: bool | None = None
    editable: bool | None = None
    enabled: bool | None = None
    visible: bool | None = None
    match: str = Field(default="first", pattern="^(first|last|best|all)$")


def _node_matches_selector(node: SemanticNode, sel: Selector) -> bool:
    """Check if a single node matches all selector predicates."""
    # text (exact) wins over text_contains
    if sel.text is not None:
        if node.text != sel.text:
            return False
    elif sel.text_contains is not None:
        if node.text is None or sel.text_contains not in node.text:
            return False

    if sel.description is not None and node.description != sel.description:
        return False

    if sel.resource_id is not None:
        # resource_id matches if exact OR ends with "/<sel.resource_id>"
        if node.resource_id is None:
            return False
        if node.resource_id != sel.resource_id and not node.resource_id.endswith(
            f"/{sel.resource_id}"
        ):
            return False

    if sel.class_name is not None and node.class_name != sel.class_name:
        return False

    if sel.clickable is not None and node.clickable != sel.clickable:
        return False
    if sel.scrollable is not None and node.scrollable != sel.scrollable:
        return False
    if sel.editable is not None and node.editable != sel.editable:
        return False
    if sel.enabled is not None and node.enabled != sel.enabled:
        return False
    if sel.visible is not None and node.visible != sel.visible:
        return False

    return True


# ---------------------------------------------------------------------------
# WaitForCondition — closed-loop polling spec
# ---------------------------------------------------------------------------


class WaitConditionType(str, Enum):
    """Closed set of wait conditions for ui.wait_for.

    Conditions are polled at poll_interval_seconds until:
      - condition satisfied → return success
      - timeout_seconds elapsed → return timeout

    Conditions:
        ui_stable           — UI tree doesn't change for stable_count
                              consecutive dumps (default 2).
                              Use after navigation/loading to wait for
                              the screen to settle.

        element_appears     — A node matching selector appears in the
                              WorldState. Use after tapping to wait for
                              the resulting UI element.

        element_disappears  — A node matching selector is no longer in
                              the WorldState. Use after dismiss actions
                              to wait for dialogs/popups to close.

        text_visible        — A node with text matching (exact or
                              contains) is visible. Simpler than
                              element_appears for quick text checks.

        keyboard_visible    — Soft keyboard is shown. Use after tapping
                              an EditText to wait for keyboard.

        keyboard_hidden     — Soft keyboard is hidden. Use after
                              pressing Back/Enter to dismiss keyboard.
    """

    UI_STABLE = "ui_stable"
    ELEMENT_APPEARS = "element_appears"
    ELEMENT_DISAPPEARS = "element_disappears"
    TEXT_VISIBLE = "text_visible"
    TEXT_DISAPPEARS = "text_disappears"
    KEYBOARD_VISIBLE = "keyboard_visible"
    KEYBOARD_HIDDEN = "keyboard_hidden"


class WaitForCondition(_UIFrozen):
    """Specification for a closed-loop wait operation.

    For element_appears/element_disappears: selector must be set.
    For text_visible/text_disappears: text must be set (exact match).
    For keyboard_visible/keyboard_hidden: no extra fields needed.
    For ui_stable: stable_count controls how many consecutive identical
        dumps are required (default 2 — i.e. dump, dump again, if
        identical → stable).

    poll_interval_seconds controls how often the runtime re-dumps the
    UI tree. Default 0.5s — aggressive enough for responsiveness,
    gentle enough to not overwhelm uiautomator (which takes 1-2s per
    dump on real devices).

    timeout_seconds is the hard limit. If exceeded, the wait returns
    with status="timeout" (NOT an exception — agents should handle this).
    """

    type: WaitConditionType
    selector: Selector | None = None
    text: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=300.0)
    poll_interval_seconds: float = Field(default=0.5, gt=0.0, le=10.0)
    stable_count: int = Field(default=2, ge=2, le=10)


class WaitForResult(_UIFrozen):
    """Result of a ui.wait_for call.

    status:
        "satisfied"  — condition met before timeout
        "timeout"    — timeout_seconds elapsed without condition met

    For "satisfied": duration_seconds tells how long it took.
    For "timeout": final_state is the last WorldState revision seen
    (for debugging).

    Both statuses are valid outcomes — agents decide how to handle
    timeout (retry, give up, ask user, etc.). The runtime does NOT
    raise on timeout — that would force agents into try/except logic.
    """

    status: str  # "satisfied" | "timeout"
    condition_type: WaitConditionType
    duration_seconds: float
    polls: int  # how many ui.dump calls were made
    final_state_revision: int | None = None
    matched_node_uuid: str | None = None  # for element_appears/text_visible


# ---------------------------------------------------------------------------
# TapElementResult — return value of ui.tap_element
# ---------------------------------------------------------------------------


class TapElementResult(_UIFrozen):
    """Result of a ui.tap_element call.

    If matched_nodes > 0 and tapped is True: success.
    If matched_nodes == 0: no element found, tap not attempted.
    If matched_nodes > 0 but tapped is False: bridge error during tap.

    tapped_node includes the full SemanticNode that was tapped, so
    agents can verify the right element was hit (text, resource_id,
    bounds for sanity check).
    """

    matched_nodes: int
    tapped: bool
    tapped_node: SemanticNode | None = None
    tap_coordinates: dict[str, int] | None = None  # {x, y}
    selector_strategy: str = "a11y"  # which strategy found the node
    duration_ms: int = 0


__all__ = [
    "Bounds",
    "NodeRole",
    "SemanticNode",
    "WorldState",
    "Selector",
    "WaitConditionType",
    "WaitForCondition",
    "WaitForResult",
    "TapElementResult",
]
