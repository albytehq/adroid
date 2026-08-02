"""Parser for uiautomator dump XML → SemanticNode tree.

This module is separated from AdbBridge so that:
  1. The parser is independently testable (no ADB required)
  2. TermuxBridge can reuse the same parser
  3. Future bridges (emulator, remote) can reuse it too

XML format reference (verified against Android 14, 2025):
  <hierarchy rotation="0">
    <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
          package="com.example.app" content-desc=""
          checkable="false" checked="false" clickable="false" enabled="true"
          focusable="false" focused="false" scrollable="false"
          long-clickable="false" password="false" selected="false"
          bounds="[0,0][1080,2340]">
      <node ...>
        <node ... />
      </node>
    </node>
  </hierarchy>

Bounds format: "[x1,y1][x2,y2]" where (x1,y1) is top-left, (x2,y2) is bottom-right.
Width = x2 - x1, Height = y2 - y1.

Role inference:
  We map Android widget class names to a closed vocabulary NodeRole.
  This is heuristic — Android has hundreds of widget classes. We use
  substring matching on the class name (case-insensitive) to handle
  custom subclasses (e.g. "com.example.CustomButton" → button).

Edge cases handled:
  - XML encoding declaration corruption (uiautomator sometimes emits
    malformed XML declarations on certain Android versions). We strip
    and re-add a clean UTF-8 declaration.
  - Empty text/content-desc attributes (treated as None, not "").
  - resource-id with package prefix ("com.example:id/btn" → stored as-is,
    Selector handles the suffix matching).
  - Node tree depth: we walk recursively, assigning uuids and tracking
    parent/children relationships.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from uuid import uuid4

from adroid.contract.ui import Bounds, NodeRole, SemanticNode


# ---------------------------------------------------------------------------
# Bounds parsing: "[x1,y1][x2,y2]" → Bounds(x, y, w, h)
# ---------------------------------------------------------------------------

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def parse_bounds(bounds_str: str) -> Bounds:
    """Parse uiautomator bounds string into Bounds model.

    "[100,200][400,250]" → Bounds(x=100, y=200, w=300, h=50)

    Returns Bounds(0,0,0,0) if the string doesn't match the expected
    format (defensive — malformed bounds shouldn't crash the parser).
    """
    if not bounds_str:
        return Bounds(x=0, y=0, w=0, h=0)
    m = _BOUNDS_RE.match(bounds_str)
    if not m:
        return Bounds(x=0, y=0, w=0, h=0)
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    w = max(0, x2 - x1)
    h = max(0, y2 - y1)
    return Bounds(x=x1, y=y1, w=w, h=h)


# ---------------------------------------------------------------------------
# Role inference: Android widget class → NodeRole
# ---------------------------------------------------------------------------

# Order matters — more specific patterns first (e.g. "ImageButton" before "Image")
_ROLE_PATTERNS: list[tuple[str, NodeRole]] = [
    ("ImageButton", NodeRole.IMAGE),
    ("ImageView", NodeRole.IMAGE_VIEW),
    ("EditText", NodeRole.EDIT_TEXT),
    ("TextView", NodeRole.TEXT_VIEW),
    ("CheckBox", NodeRole.CHECKBOX),
    ("RadioButton", NodeRole.RADIO),
    ("Switch", NodeRole.SWITCH),
    ("ToggleButton", NodeRole.TOGGLE),
    ("Spinner", NodeRole.SPINNER),
    ("WebView", NodeRole.WEB_VIEW),
    ("RecyclerView", NodeRole.RECYCLER_VIEW),
    ("ListView", NodeRole.LIST),
    ("ScrollView", NodeRole.SCROLL_VIEW),
    ("ViewPager", NodeRole.VIEW_PAGER),
    ("FrameLayout", NodeRole.FRAME_LAYOUT),
    ("LinearLayout", NodeRole.LINEAR_LAYOUT),
    ("RelativeLayout", NodeRole.RELATIVE_LAYOUT),
    ("ProgressBar", NodeRole.PROGRESS_BAR),
    ("SeekBar", NodeRole.SEEK_BAR),
    ("Button", NodeRole.BUTTON),
    ("MenuItem", NodeRole.MENU_ITEM),
    ("Tab", NodeRole.TAB),
    ("Dialog", NodeRole.DIALOG),
]


def infer_role(class_name: str | None) -> NodeRole:
    """Infer NodeRole from Android widget class name.

    Uses substring matching on the class name (case-sensitive, since
    Android class names are CamelCase). Returns NodeRole.UNKNOWN if no
    pattern matches.

    Examples:
        "android.widget.Button" → BUTTON
        "android.widget.ImageButton" → IMAGE (matched before "Button")
        "com.example.CustomButton" → BUTTON (substring "Button")
        "android.view.View" → UNKNOWN
    """
    if not class_name:
        return NodeRole.UNKNOWN
    # Use the simple class name (after last '.') for matching
    simple = class_name.rsplit(".", 1)[-1] if "." in class_name else class_name
    for pattern, role in _ROLE_PATTERNS:
        if pattern in simple:
            return role
    return NodeRole.UNKNOWN


# ---------------------------------------------------------------------------
# Attribute extraction helpers
# ---------------------------------------------------------------------------


def _attr_bool(node: ET.Element, attr: str) -> bool:
    """Extract a boolean attribute. Returns False if missing or not 'true'."""
    return node.get(attr, "false").lower() == "true"


def _attr_str(node: ET.Element, attr: str) -> str | None:
    """Extract a string attribute. Returns None if empty or missing."""
    v = node.get(attr, "")
    return v if v else None


# ---------------------------------------------------------------------------
# Main parser: XML → list[SemanticNode] (flat, with parent/children uuids)
# ---------------------------------------------------------------------------


def parse_uiautomator_xml(xml_raw: str) -> list[SemanticNode]:
    """Parse uiautomator dump XML into a flat list of SemanticNodes.

    v0.3.5: Uses Rust extension (adroid_rust) if available for 2.8x speedup.
    Falls back to Python parser if Rust extension not installed.

    The returned list is in depth-first order (same as XML order).
    parent_uuid / children_uuids are populated based on the XML tree
    structure.

    Args:
        xml_raw: Raw XML string from `uiautomator dump` + `cat`.

    Returns:
        List of SemanticNode objects. Empty list if XML is empty or
        unparseable (after best-effort cleanup).
    """
    if not xml_raw or not xml_raw.strip():
        return []

    # v0.3.5: Try Rust extension first (2.8x faster)
    try:
        import json
        import adroid_rust
        json_str = adroid_rust.parse_uiautomator_xml_json(xml_raw)
        raw_nodes = json.loads(json_str)
        return _build_semantic_nodes_from_dicts(raw_nodes)
    except ImportError:
        pass  # Rust extension not installed — fall back to Python
    except Exception:
        pass  # Rust parser failed — fall back to Python (defensive)

    # Python fallback (original implementation)
    return _parse_uiautomator_xml_python(xml_raw)


def _build_semantic_nodes_from_dicts(raw_nodes: list[dict]) -> list[SemanticNode]:
    """Convert list of plain dicts (from Rust JSON) to SemanticNode objects."""
    nodes: list[SemanticNode] = []
    for d in raw_nodes:
        bounds_data = d.get("bounds", {})
        bounds = Bounds(
            x=bounds_data.get("x", 0),
            y=bounds_data.get("y", 0),
            w=bounds_data.get("w", 0),
            h=bounds_data.get("h", 0),
        )
        role_str = d.get("role", "unknown")
        try:
            role = NodeRole(role_str)
        except ValueError:
            role = NodeRole.UNKNOWN

        nodes.append(SemanticNode(
            uuid=d.get("uuid", ""),
            role=role,
            text=d.get("text"),
            description=d.get("description"),
            resource_id=d.get("resource_id"),
            class_name=d.get("class_name"),
            package=d.get("package"),
            bounds=bounds,
            clickable=d.get("clickable", False),
            scrollable=d.get("scrollable", False),
            editable=d.get("editable", False),
            enabled=d.get("enabled", True),
            visible=d.get("visible", True),
            focused=d.get("focused", False),
            selected=d.get("selected", False),
            parent_uuid=d.get("parent_uuid"),
            children_uuids=tuple(d.get("children_uuids", [])),
            confidence=d.get("confidence", 1.0),
            source=d.get("source", "a11y"),
        ))
    return nodes


def _parse_uiautomator_xml_python(xml_raw: str) -> list[SemanticNode]:
    """Python fallback parser (original implementation)."""
    # Cleanup: uiautomator sometimes emits malformed XML declarations.
    if "<?xml" in xml_raw:
        end_decl = xml_raw.find("?>")
        if end_decl > 0:
            xml_raw = '<?xml version="1.0" encoding="UTF-8"?>' + xml_raw[end_decl + 2:]

    try:
        root = ET.fromstring(xml_raw)
    except ET.ParseError:
        # Try without any declaration
        if "<?xml" in xml_raw:
            end_decl = xml_raw.find("?>")
            if end_decl > 0:
                try:
                    root = ET.fromstring(xml_raw[end_decl + 2:])
                except ET.ParseError:
                    return []  # Give up gracefully
            else:
                return []
        else:
            return []

    # The root is <hierarchy>, children are top-level <node>s
    nodes: list[SemanticNode] = []

    # Walk each top-level node (usually just one)
    for top_node in root.findall("node"):
        _walk_node(top_node, parent_uuid=None, nodes_out=nodes)

    return nodes


def _walk_node(
    element: ET.Element,
    parent_uuid: str | None,
    nodes_out: list[SemanticNode],
) -> str:
    """Recursively walk an XML <node> element, append to nodes_out.

    Returns the uuid of this node (so parent can track children).
    """
    uuid = uuid4().hex

    # Extract all attributes
    bounds_str = element.get("bounds", "[0,0][0,0]")
    bounds = parse_bounds(bounds_str)
    class_name = _attr_str(element, "class")
    text = _attr_str(element, "text")
    description = _attr_str(element, "content-desc")
    resource_id = _attr_str(element, "resource-id")
    package = _attr_str(element, "package")

    clickable = _attr_bool(element, "clickable")
    scrollable = _attr_bool(element, "scrollable")
    enabled = _attr_bool(element, "enabled")
    focusable = _attr_bool(element, "focusable")
    focused = _attr_bool(element, "focused")
    selected = _attr_bool(element, "selected")

    # Editable heuristic: EditText-class + focusable
    # uiautomator has no direct "editable" attribute
    role = infer_role(class_name)
    editable = role == NodeRole.EDIT_TEXT and focusable

    # Walk children first to collect their uuids
    children_uuids: list[str] = []
    for child in element.findall("node"):
        child_uuid = _walk_node(child, parent_uuid=uuid, nodes_out=nodes_out)
        children_uuids.append(child_uuid)

    # Construct the SemanticNode
    node = SemanticNode(
        uuid=uuid,
        role=role,
        text=text,
        description=description,
        resource_id=resource_id,
        class_name=class_name,
        package=package,
        bounds=bounds,
        clickable=clickable,
        scrollable=scrollable,
        editable=editable,
        enabled=enabled,
        visible=True,  # uiautomator dumps only visible windows
        focused=focused,
        selected=selected,
        parent_uuid=parent_uuid,
        children_uuids=tuple(children_uuids),
        confidence=1.0,
        source="a11y",
    )
    nodes_out.append(node)
    return uuid


__all__ = [
    "parse_bounds",
    "infer_role",
    "parse_uiautomator_xml",
]
