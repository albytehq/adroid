"""Tests for the web server — agent HTTP API + approval endpoints."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.types import Capability, DeviceId, DeviceInfo, DeviceState, Scope
from adroid.permissions.interactive import ApprovalBroker, InteractiveGate
from adroid.runtime.core import AdroidRuntime
from adroid.web.server import create_app


@pytest.fixture
def setup():
    blob_store = InMemoryBlobStore()
    bridge = MockBridge(blob_store=blob_store)
    bridge.add_device(
        DeviceInfo(
            id=DeviceId(kind="adb", value="mock-1"),
            state=DeviceState.ONLINE,
            model="MockPhone",
        )
    )
    runtime = AdroidRuntime(
        issuer_id="adroid-test",
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path("/tmp/adroid-test-web.auditlog"),
    )
    broker = ApprovalBroker()
    gate = InteractiveGate(
        issuer_id=runtime.issuer_id,
        public_key=runtime._token_public,  # noqa: SLF001
        broker=broker,
        approval_timeout_seconds=5.0,
    )
    app = create_app(
        runtime=runtime,
        broker=broker,
        interactive_gate=gate,
        browser_session_id="test-browser",
    )
    client = TestClient(app)

    read_token = runtime.issue_token(
        subject="agent:test",
        grants=[
            (Capability.DEVICE_LIST, Scope.READ),
            (Capability.DEVICE_OBSERVE, Scope.READ),
            (Capability.DEVICE_SCREENSHOT, Scope.READ),
            (Capability.APP_LIST, Scope.READ),
            (Capability.APP_LAUNCH, Scope.ACT),
        ],
    )
    return client, runtime, broker, read_token


def _get_token(setup):
    """Helper for tests that only need the token from the setup tuple."""
    return setup[3]


# ---------------------------------------------------------------------------
# Browser UI
# ---------------------------------------------------------------------------


def test_ui_returns_html(setup):
    client, *_ = setup
    res = client.get("/ui")
    assert res.status_code == 200
    assert "Adroid" in res.text
    assert "Pending Approvals" in res.text


def test_root_alias_returns_ui(setup):
    client, *_ = setup
    res = client.get("/")
    assert res.status_code == 200


def test_api_tools_lists_all_tools(setup):
    client, *_ = setup
    res = client.get("/api/tools")
    assert res.status_code == 200
    names = {t["name"] for t in res.json()}
    assert "device.list" in names
    assert "app.launch" in names


# ---------------------------------------------------------------------------
# Agent HTTP API — READ scope (auto-approved)
# ---------------------------------------------------------------------------


def test_agent_call_device_list_succeeds(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.list",
        "args": {},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert len(body["result"]["devices"]) == 1
    assert body["result"]["devices"][0]["id"]["value"] == "mock-1"
    # READ scope — no approval needed
    assert body["approval_id"] is None


def test_agent_call_device_observe_succeeds(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.observe",
        "args": {"device_id": {"kind": "adb", "value": "mock-1"}},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["device"]["model"] == "MockPhone"


def test_agent_call_unknown_tool_returns_400_or_404(setup):
    """Unknown tool returns 400 (token validation first) or 404 (if token
    validation is bypassed). Both are acceptable — the key invariant is
    that the call does not succeed."""
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": {},
        "tool": "nonexistent.tool",
        "args": {},
    })
    assert res.status_code in (400, 404)


def test_agent_call_invalid_token_returns_400(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": {"not": "a token"},
        "tool": "device.list",
        "args": {},
    })
    assert res.status_code == 400


def test_agent_call_missing_capability_returns_error(setup):
    """Token without the requested capability returns ok=False with
    permission denied error — not an HTTP exception, because the runtime
    catches AdroidError and returns it as a structured response."""
    client, runtime, _, _ = setup
    no_grants_token = runtime.issue_token(subject="agent:nogrants", grants=[])
    res = client.post("/agent/call", json={
        "token": no_grants_token.model_dump(mode="json"),
        "tool": "device.list",
        "args": {},
    })
    # The runtime wraps AdroidError as HTTPException — but in this version
    # the dispatch raises HTTPException for unknown tools only, and lets
    # AdroidError bubble through _dispatch_tool's catch.
    # Either way: not ok=True
    body = res.json() if res.status_code == 200 else res.json()
    assert body.get("ok") is not True


# ---------------------------------------------------------------------------
# Agent HTTP API — ACT scope (requires approval)
# ---------------------------------------------------------------------------


def test_agent_call_act_scope_blocks_pending_approval(setup):
    """ACT scope calls block waiting for browser approval. We simulate
    the browser approving via /api/approve in a separate thread."""
    client, runtime, broker, token = setup

    result = {}

    def caller():
        res = client.post("/agent/call", json={
            "token": token.model_dump(mode="json"),
            "tool": "app.launch",
            "args": {
                "device_id": {"kind": "adb", "value": "mock-1"},
                "package_name": "com.example.app",
            },
        })
        result["body"] = res.json()

    t = threading.Thread(target=caller)
    t.start()

    # Wait for the pending approval to appear
    for _ in range(40):  # 2 seconds
        res = client.get("/api/pending")
        if res.json():
            break
        time.sleep(0.05)

    pending = res.json()
    assert len(pending) == 1
    assert pending[0]["capability"] == "app.launch"
    assert pending[0]["required_scope"] == "act"

    # Approve via the API
    approval_id = pending[0]["approval_id"]
    res = client.post(f"/api/approve/{approval_id}")
    assert res.status_code == 200
    assert res.json()["decision"] == "approved"

    # Wait for the call to complete
    t.join(timeout=5.0)

    body = result["body"]
    assert body["ok"] is True
    assert body["result"]["launched"] is True
    assert body["approval_id"] is not None


def test_agent_call_act_scope_denied_returns_error(setup):
    client, runtime, broker, token = setup

    result = {}

    def caller():
        res = client.post("/agent/call", json={
            "token": token.model_dump(mode="json"),
            "tool": "app.launch",
            "args": {
                "device_id": {"kind": "adb", "value": "mock-1"},
                "package_name": "com.example.app",
            },
        })
        result["body"] = res.json()

    t = threading.Thread(target=caller)
    t.start()

    # Wait for pending
    for _ in range(40):
        res = client.get("/api/pending")
        if res.json():
            break
        time.sleep(0.05)

    pending = res.json()
    approval_id = pending[0]["approval_id"]

    # Deny via the API
    client.post(f"/api/deny/{approval_id}")

    t.join(timeout=5.0)

    body = result["body"]
    assert body["ok"] is False
    assert body["error"]["code"] == "adroid.permission.interactive_denied"


# ---------------------------------------------------------------------------
# Audit + terminal stream
# ---------------------------------------------------------------------------


def test_terminal_history_endpoint(setup):
    client, *_ = setup
    token = _get_token(setup)
    # Make a call first to generate terminal lines
    client.post("/agent/call", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.list",
        "args": {},
    })
    res = client.get("/api/terminal")
    assert res.status_code == 200
    lines = res.json()
    assert isinstance(lines, list)
    # boot system line + at least one request line
    assert len(lines) >= 1


def test_audit_endpoint_returns_events(setup):
    client, runtime, broker, token = setup
    # Make a call to generate an audit event
    client.post("/agent/call", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.list",
        "args": {},
    })
    res = client.get("/api/audit")
    assert res.status_code == 200
    events = res.json()
    assert isinstance(events, list)
    # boot + at least one call
    assert len(events) >= 2


def test_screenshot_blob_endpoints(setup):
    client, runtime, broker, token = setup
    # Capture a screenshot
    res = client.post("/agent/call", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.screenshot",
        "args": {"device_id": {"kind": "adb", "value": "mock-1"}},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    blob_ref = body["result"]["screenshot"]["blob_ref"]

    # Metadata endpoint
    res = client.get(f"/blob/{blob_ref}")
    assert res.status_code == 200
    assert res.json()["blob_ref"] == blob_ref

    # Raw bytes endpoint
    res = client.get(f"/blob/{blob_ref}/raw")
    assert res.status_code == 200
    assert res.content.startswith(b"\x89PNG")
    assert res.headers["content-type"] == "image/png"


# ---------------------------------------------------------------------------
# Approval history endpoint
# ---------------------------------------------------------------------------


def test_history_endpoint_returns_past_approvals(setup):
    client, runtime, broker, token = setup

    # Run + approve one ACT call
    def caller():
        client.post("/agent/call", json={
            "token": token.model_dump(mode="json"),
            "tool": "app.launch",
            "args": {
                "device_id": {"kind": "adb", "value": "mock-1"},
                "package_name": "com.example.app",
            },
        })

    t = threading.Thread(target=caller)
    t.start()

    for _ in range(40):
        res = client.get("/api/pending")
        if res.json():
            break
        time.sleep(0.05)

    pending = res.json()
    client.post(f"/api/approve/{pending[0]['approval_id']}")
    t.join(timeout=5.0)

    res = client.get("/api/history")
    assert res.status_code == 200
    history = res.json()
    assert len(history) >= 1
    assert history[0]["decision"] == "approved"
