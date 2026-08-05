"""Tests for v0.2.0 Semantic UI tools.

Tests cover:
  - uiautomator_parser: XML → SemanticNode parsing
  - contract/ui.py: WorldState, Selector, WaitForCondition models
  - Mock bridge: ui_dump, ui_find_elements, ui_tap_element, ui_wait_for
  - Web API: all 4 new tools via /agent/call
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.bridge.uiautomator_parser import (
    infer_role,
    parse_bounds,
    parse_uiautomator_xml,
)
from adroid.contract.ui import (
    Bounds,
    NodeRole,
    Selector,
    SemanticNode,
    TapElementResult,
    WaitConditionType,
    WaitForCondition,
    WaitForResult,
    WorldState,
)
from adroid.contract.types import Capability, DeviceId, DeviceInfo, DeviceState, Scope
from adroid.permissions.sessions import SessionManager
from adroid.permissions.tokens import TokenIssuer
from adroid.runtime.core import AdroidRuntime
from adroid.web.server import create_app


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestBoundsParsing:
    def test_parse_valid_bounds(self):
        b = parse_bounds("[100,200][400,250]")
        assert b.x == 100
        assert b.y == 200
        assert b.w == 300
        assert b.h == 50

    def test_parse_zero_bounds(self):
        b = parse_bounds("[0,0][0,0]")
        assert b.x == 0 and b.y == 0 and b.w == 0 and b.h == 0

    def test_parse_empty_string(self):
        b = parse_bounds("")
        assert b.x == 0 and b.y == 0 and b.w == 0 and b.h == 0

    def test_parse_invalid_format(self):
        b = parse_bounds("not-a-bounds")
        assert b.x == 0 and b.y == 0 and b.w == 0 and b.h == 0

    def test_bounds_center(self):
        b = Bounds(x=100, y=200, w=300, h=50)
        assert b.center == (250, 225)

    def test_bounds_contains(self):
        b = Bounds(x=100, y=200, w=300, h=50)
        assert b.contains(250, 225) is True
        assert b.contains(100, 200) is True  # top-left corner
        assert b.contains(399, 249) is True  # bottom-right (exclusive)
        assert b.contains(50, 225) is False  # outside left
        assert b.contains(500, 225) is False  # outside right


class TestRoleInference:
    def test_button(self):
        assert infer_role("android.widget.Button") == NodeRole.BUTTON

    def test_edit_text(self):
        assert infer_role("android.widget.EditText") == NodeRole.EDIT_TEXT

    def test_image_button_matches_image_not_button(self):
        # ImageButton should match IMAGE pattern (before BUTTON)
        assert infer_role("android.widget.ImageButton") == NodeRole.IMAGE

    def test_custom_button_subclass(self):
        assert infer_role("com.example.CustomButton") == NodeRole.BUTTON

    def test_unknown_class(self):
        assert infer_role("android.view.View") == NodeRole.UNKNOWN

    def test_none_class(self):
        assert infer_role(None) == NodeRole.UNKNOWN

    def test_empty_class(self):
        assert infer_role("") == NodeRole.UNKNOWN

    def test_text_view(self):
        assert infer_role("android.widget.TextView") == NodeRole.TEXT_VIEW

    def test_recycler_view(self):
        assert infer_role("androidx.recyclerview.widget.RecyclerView") == NodeRole.RECYCLER_VIEW

    def test_checkbox(self):
        assert infer_role("android.widget.CheckBox") == NodeRole.CHECKBOX


class TestXmlParser:
    SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout"
        package="com.example.app" content-desc=""
        checkable="false" checked="false" clickable="false" enabled="true"
        focusable="false" focused="false" scrollable="false"
        long-clickable="false" password="false" selected="false"
        bounds="[0,0][1080,2340]">
    <node index="0" text="Search" resource-id="com.example.app:id/search_btn"
          class="android.widget.Button" package="com.example.app" content-desc="Search button"
          checkable="false" checked="false" clickable="true" enabled="true"
          focusable="true" focused="false" scrollable="false"
          long-clickable="false" password="false" selected="false"
          bounds="[936,72][1008,144]" />
    <node index="1" text="Username" resource-id="com.example.app:id/username_field"
          class="android.widget.EditText" package="com.example.app" content-desc=""
          checkable="false" checked="false" clickable="true" enabled="true"
          focusable="true" focused="true" scrollable="false"
          long-clickable="false" password="false" selected="false"
          bounds="[100,500][980,650]" />
  </node>
</hierarchy>"""

    def test_parse_returns_nodes(self):
        nodes = parse_uiautomator_xml(self.SAMPLE_XML)
        assert len(nodes) == 3  # FrameLayout + Button + EditText

    def test_parse_empty_xml(self):
        nodes = parse_uiautomator_xml("")
        assert nodes == []

    def test_parse_whitespace_only(self):
        nodes = parse_uiautomator_xml("   \n  ")
        assert nodes == []

    def test_node_tree_structure(self):
        nodes = parse_uiautomator_xml(self.SAMPLE_XML)
        # Find nodes by role (order may vary due to recursive walk)
        parent = next(n for n in nodes if n.role == NodeRole.FRAME_LAYOUT)
        btn = next(n for n in nodes if n.role == NodeRole.BUTTON)
        edit = next(n for n in nodes if n.role == NodeRole.EDIT_TEXT)

        # Parent has 2 children
        assert len(parent.children_uuids) == 2
        assert btn.uuid in parent.children_uuids
        assert edit.uuid in parent.children_uuids

        # Children point back to parent
        assert btn.parent_uuid == parent.uuid
        assert edit.parent_uuid == parent.uuid

    def test_button_attributes(self):
        nodes = parse_uiautomator_xml(self.SAMPLE_XML)
        btn = next(n for n in nodes if n.role == NodeRole.BUTTON)
        assert btn.text == "Search"
        assert btn.description == "Search button"
        assert btn.resource_id == "com.example.app:id/search_btn"
        assert btn.clickable is True
        assert btn.focused is False
        assert btn.bounds.center == (972, 108)

    def test_edit_text_editable(self):
        nodes = parse_uiautomator_xml(self.SAMPLE_XML)
        edit = next(n for n in nodes if n.role == NodeRole.EDIT_TEXT)
        assert edit.editable is True
        assert edit.focused is True

    def test_malformed_xml_declaration_handled(self):
        """uiautomator sometimes emits malformed XML declarations.
        The parser should clean them up."""
        malformed = '<?xml version="1.0" encoding="UTF-8" badattr="x"?>\n<hierarchy><node class="android.widget.Button" bounds="[0,0][10,10]" /></hierarchy>'
        nodes = parse_uiautomator_xml(malformed)
        assert len(nodes) == 1
        assert nodes[0].role == NodeRole.BUTTON


# ---------------------------------------------------------------------------
# Selector + WorldState query tests
# ---------------------------------------------------------------------------


class TestSelectorMatching:
    @pytest.fixture
    def sample_worldstate(self):
        nodes = (
            SemanticNode(
                uuid="root",
                role=NodeRole.FRAME_LAYOUT,
                bounds=Bounds(x=0, y=0, w=1080, h=2340),
            ),
            SemanticNode(
                uuid="btn1",
                role=NodeRole.BUTTON,
                text="Search",
                resource_id="com.app:id/search_btn",
                bounds=Bounds(x=936, y=72, w=72, h=72),
                clickable=True,
                parent_uuid="root",
            ),
            SemanticNode(
                uuid="btn2",
                role=NodeRole.BUTTON,
                text="Cancel",
                resource_id="com.app:id/cancel_btn",
                bounds=Bounds(x=100, y=72, w=72, h=72),
                clickable=True,
                parent_uuid="root",
            ),
            SemanticNode(
                uuid="edit1",
                role=NodeRole.EDIT_TEXT,
                text="Username",
                resource_id="com.app:id/username",
                bounds=Bounds(x=100, y=500, w=880, h=150),
                clickable=True,
                editable=True,
                parent_uuid="root",
            ),
        )
        return WorldState(revision=1, nodes=nodes, foreground_package="com.app")

    def test_find_by_text_exact(self, sample_worldstate):
        sel = Selector(text="Search")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1
        assert matches[0].text == "Search"

    def test_find_by_text_no_match(self, sample_worldstate):
        sel = Selector(text="NonExistent")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 0

    def test_find_by_text_contains(self, sample_worldstate):
        sel = Selector(text_contains="Search")  # matches "Search"
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1

    def test_find_by_resource_id_full(self, sample_worldstate):
        sel = Selector(resource_id="com.app:id/search_btn")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1

    def test_find_by_resource_id_suffix(self, sample_worldstate):
        # Selector matches if resource_id ends with "/<sel>"
        sel = Selector(resource_id="search_btn")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1
        assert matches[0].resource_id == "com.app:id/search_btn"

    def test_find_by_clickable(self, sample_worldstate):
        sel = Selector(clickable=True, match="all")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 3  # btn1, btn2, edit1 (root is not clickable)

    def test_find_by_role_combo(self, sample_worldstate):
        sel = Selector(clickable=True, text="Username")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1
        assert matches[0].text == "Username"

    def test_find_match_first(self, sample_worldstate):
        sel = Selector(clickable=True, match="first")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1

    def test_find_match_all(self, sample_worldstate):
        sel = Selector(clickable=True, match="all")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 3

    def test_find_match_last(self, sample_worldstate):
        sel = Selector(clickable=True, match="last")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1
        # Last clickable in tree order is edit1
        assert matches[0].text == "Username"

    def test_find_match_best(self, sample_worldstate):
        # "best" = longest text
        sel = Selector(clickable=True, match="best")
        matches = sample_worldstate.find(sel)
        assert len(matches) == 1
        assert matches[0].text == "Username"  # longest text among clickable

    def test_worldstate_node_count(self, sample_worldstate):
        assert sample_worldstate.node_count == 4

    def test_worldstate_clickable_nodes(self, sample_worldstate):
        clickable = sample_worldstate.clickable_nodes
        assert len(clickable) == 3


# ---------------------------------------------------------------------------
# Mock bridge UI tests
# ---------------------------------------------------------------------------


class TestMockBridgeUI:
    @pytest.fixture
    def bridge(self):
        blob_store = InMemoryBlobStore()
        b = MockBridge(blob_store=blob_store)
        b.add_device(
            DeviceInfo(
                id=DeviceId(kind="adb", value="mock-1"),
                state=DeviceState.ONLINE,
                model="MockPhone",
            )
        )
        return b

    def test_ui_dump_returns_worldstate(self, bridge):
        from adroid.contract.ui import WorldState
        ws = bridge.ui_dump(DeviceId(kind="adb", value="mock-1"))
        assert isinstance(ws, WorldState)
        assert ws.node_count > 0
        assert ws.foreground_package == "com.mock.app"
        assert ws.screen_dimensions["width"] == 1080

    def test_ui_dump_has_search_button(self, bridge):
        ws = bridge.ui_dump(DeviceId(kind="adb", value="mock-1"))
        search_nodes = [n for n in ws.nodes if n.text == "Search"]
        assert len(search_nodes) == 1
        assert search_nodes[0].clickable is True

    def test_ui_find_elements_by_text(self, bridge):
        sel = Selector(text="Search")
        matches = bridge.ui_find_elements(DeviceId(kind="adb", value="mock-1"), sel)
        assert len(matches) == 1
        assert matches[0].text == "Search"

    def test_ui_find_elements_no_match(self, bridge):
        sel = Selector(text="NonExistent")
        matches = bridge.ui_find_elements(DeviceId(kind="adb", value="mock-1"), sel)
        assert len(matches) == 0

    def test_ui_tap_element_success(self, bridge):
        sel = Selector(text="Search")
        result = bridge.ui_tap_element(DeviceId(kind="adb", value="mock-1"), sel)
        assert result.matched_nodes == 1
        assert result.tapped is True
        assert result.tapped_node is not None
        assert result.tapped_node.text == "Search"
        assert result.tap_coordinates is not None
        # Verify tap was recorded in mock state
        did = DeviceId(kind="adb", value="mock-1")
        with bridge._lock:
            assert bridge._devices[str(did)].last_tap is not None

    def test_ui_tap_element_no_match(self, bridge):
        sel = Selector(text="NonExistent")
        result = bridge.ui_tap_element(DeviceId(kind="adb", value="mock-1"), sel)
        assert result.matched_nodes == 0
        assert result.tapped is False
        assert result.tapped_node is None

    def test_ui_wait_for_returns_satisfied(self, bridge):
        cond = WaitForCondition(
            type=WaitConditionType.UI_STABLE,
            timeout_seconds=5.0,
            poll_interval_seconds=0.1,
        )
        result = bridge.ui_wait_for(DeviceId(kind="adb", value="mock-1"), cond)
        assert result.status == "satisfied"
        assert result.polls >= 1

    def test_ui_wait_for_keyboard_visible(self, bridge):
        # Mock bridge has keyboard_visible=True
        cond = WaitForCondition(
            type=WaitConditionType.KEYBOARD_VISIBLE,
            timeout_seconds=5.0,
        )
        result = bridge.ui_wait_for(DeviceId(kind="adb", value="mock-1"), cond)
        assert result.status == "satisfied"

    def test_ui_wait_for_element_appears(self, bridge):
        cond = WaitForCondition(
            type=WaitConditionType.ELEMENT_APPEARS,
            selector=Selector(text="Search"),
            timeout_seconds=5.0,
        )
        result = bridge.ui_wait_for(DeviceId(kind="adb", value="mock-1"), cond)
        assert result.status == "satisfied"
        assert result.matched_node_uuid is not None


# ---------------------------------------------------------------------------
# Web API tests
# ---------------------------------------------------------------------------


@pytest.fixture
def web_setup():
    blob_store = InMemoryBlobStore()
    bridge = MockBridge(blob_store=blob_store)
    bridge.add_device(
        DeviceInfo(
            id=DeviceId(kind="adb", value="mock-1"),
            state=DeviceState.ONLINE,
            model="MockPhone",
        )
    )
    # Delete stale audit log so signature verification works on fresh chain.
    Path("/tmp/adroid-test-ui.auditlog").unlink(missing_ok=True)
    runtime = AdroidRuntime(
        issuer_id="adroid-test",
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path("/tmp/adroid-test-ui.auditlog"),
    )
    issuer = TokenIssuer(
        issuer_id=runtime.issuer_id,
        signing_key=runtime._token_private,
        public_key=runtime._token_public,
    )
    session_manager = SessionManager(issuer=issuer, max_sessions=20)
    from datetime import timedelta
    bootstrap_token = issuer.issue(subject="bootstrap", grants=[], ttl=timedelta(days=30))
    app = create_app(
        runtime=runtime,
        session_manager=session_manager,
        issuer=issuer,
        bootstrap_token=bootstrap_token,
        browser_session_id="test-browser",
    )
    client = TestClient(app)
    # Hit /ui first to set the browser-session cookie for /api/* auth.
    client.get("/ui")

    # Pair a session
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap_token.model_dump(mode="json"),
        "agent_id": "agent:ui-test",
    })
    pairing_id = res.json()["pairing_id"]
    client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": 3600})
    res = client.get(f"/agent/session/{pairing_id}")
    session_token = res.json()["session_token"]
    return client, session_token


DEVICE_ID = {"kind": "adb", "value": "mock-1"}


class TestWebUIAPI:
    def test_all_17_tools_listed(self, web_setup):
        """v0.2.0 should have 17 tools (13 from v0.1.0 + 4 new UI tools)."""
        client, _ = web_setup
        res = client.get("/api/tools")
        names = {t["name"] for t in res.json()}
        assert "ui.dump" in names
        assert "ui.find_elements" in names
        assert "ui.tap_element" in names
        assert "ui.wait_for" in names

    def test_ui_dump_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.dump",
            "args": {"device_id": DEVICE_ID},
        })
        body = res.json()
        assert body["ok"] is True
        ws = body["result"]["world_state"]
        assert ws["revision"] >= 1
        assert len(ws["nodes"]) > 0
        assert ws["foreground_package"] == "com.mock.app"
        assert ws["screen_dimensions"]["width"] == 1080

    def test_ui_find_elements_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.find_elements",
            "args": {
                "device_id": DEVICE_ID,
                "selector": {"text": "Search"},
            },
        })
        body = res.json()
        assert body["ok"] is True
        elements = body["result"]["elements"]
        assert len(elements) == 1
        assert elements[0]["text"] == "Search"
        assert elements[0]["clickable"] is True

    def test_ui_find_elements_no_match_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.find_elements",
            "args": {
                "device_id": DEVICE_ID,
                "selector": {"text": "NonExistent"},
            },
        })
        body = res.json()
        assert body["ok"] is True
        assert len(body["result"]["elements"]) == 0

    def test_ui_tap_element_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.tap_element",
            "args": {
                "device_id": DEVICE_ID,
                "selector": {"text": "Search"},
            },
        })
        body = res.json()
        assert body["ok"] is True
        assert body["result"]["matched_nodes"] == 1
        assert body["result"]["tapped"] is True
        assert body["result"]["tapped_node"]["text"] == "Search"

    def test_ui_tap_element_no_match_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.tap_element",
            "args": {
                "device_id": DEVICE_ID,
                "selector": {"text": "NonExistent"},
            },
        })
        body = res.json()
        assert body["ok"] is True
        assert body["result"]["matched_nodes"] == 0
        assert body["result"]["tapped"] is False

    def test_ui_wait_for_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.wait_for",
            "args": {
                "device_id": DEVICE_ID,
                "condition": {
                    "type": "ui_stable",
                    "timeout_seconds": 5.0,
                    "poll_interval_seconds": 0.1,
                },
            },
        })
        body = res.json()
        assert body["ok"] is True
        assert body["result"]["status"] == "satisfied"
        assert body["result"]["polls"] >= 1

    def test_ui_wait_for_element_appears_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.wait_for",
            "args": {
                "device_id": DEVICE_ID,
                "condition": {
                    "type": "element_appears",
                    "selector": {"text": "Search"},
                    "timeout_seconds": 5.0,
                },
            },
        })
        body = res.json()
        assert body["ok"] is True
        assert body["result"]["status"] == "satisfied"

    def test_ui_wait_for_keyboard_visible_via_api(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.wait_for",
            "args": {
                "device_id": DEVICE_ID,
                "condition": {"type": "keyboard_visible", "timeout_seconds": 5.0},
            },
        })
        body = res.json()
        assert body["ok"] is True
        assert body["result"]["status"] == "satisfied"

    def test_ui_find_elements_with_resource_id_suffix(self, web_setup):
        """Selector resource_id matches if node's resource_id ends with /<sel>."""
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.find_elements",
            "args": {
                "device_id": DEVICE_ID,
                "selector": {"resource_id": "search_btn"},  # short form
            },
        })
        body = res.json()
        assert body["ok"] is True
        elements = body["result"]["elements"]
        assert len(elements) == 1
        assert elements[0]["resource_id"] == "com.mock.app:id/search_btn"

    def test_ui_find_elements_with_clickable_filter(self, web_setup):
        client, token = web_setup
        res = client.post("/agent/call", json={
            "token": token,
            "tool": "ui.find_elements",
            "args": {
                "device_id": DEVICE_ID,
                "selector": {"clickable": True, "match": "all"},
            },
        })
        body = res.json()
        assert body["ok"] is True
        elements = body["result"]["elements"]
        # Mock has Search button, Sign in button, Username field — all clickable
        assert len(elements) >= 3
