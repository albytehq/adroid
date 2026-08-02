"""Tests for the async agent API — /agent/call/async + /agent/result/{id}.

Async flow is critical for chat-based AI agents (like me) that can't
hold long-lived HTTP connections. The agent POSTs, gets back an
approval_id + approval_url, shares the URL with the human, then polls
/agent/result/{id} until state is terminal.
"""

from __future__ import annotations

import threading
import time

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
        audit_log_path=Path("/tmp/adroid-test-async.auditlog"),
    )
    broker = ApprovalBroker()
    gate = InteractiveGate(
        issuer_id=runtime.issuer_id,
        public_key=runtime._token_public,  # noqa: SLF001
        broker=broker,
        approval_timeout_seconds=10.0,
    )
    app = create_app(
        runtime=runtime,
        broker=broker,
        interactive_gate=gate,
        browser_session_id="test-browser",
    )
    client = TestClient(app)
    token = runtime.issue_token(
        subject="agent:async-test",
        grants=[
            (Capability.DEVICE_LIST, Scope.READ),
            (Capability.DEVICE_OBSERVE, Scope.READ),
            (Capability.DEVICE_SCREENSHOT, Scope.READ),
            (Capability.APP_LIST, Scope.READ),
            (Capability.APP_LAUNCH, Scope.ACT),
        ],
    )
    return client, runtime, broker, token


# Need Path import
from pathlib import Path  # noqa: E402


# ---------------------------------------------------------------------------
# READ scope — async returns completed immediately
# ---------------------------------------------------------------------------


def test_async_read_scope_returns_completed(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.list",
        "args": {},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "completed"
    assert len(body["result"]["devices"]) == 1
    assert body["approval_id"] is None
    assert body["approval_url"] is None


def test_async_read_scope_observe(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.observe",
        "args": {"device_id": {"kind": "adb", "value": "mock-1"}},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "completed"
    assert body["result"]["device"]["model"] == "MockPhone"


# ---------------------------------------------------------------------------
# ACT scope — async returns pending_approval, then agent polls
# ---------------------------------------------------------------------------


def test_async_act_scope_returns_pending_approval(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "app.launch",
        "args": {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "package_name": "com.example.app",
        },
    })
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "pending_approval"
    assert body["approval_id"] is not None
    assert body["approval_url"] is not None
    # URL should point to /ui#approval-{id}
    assert body["approval_id"] in body["approval_url"]
    assert "/ui#approval-" in body["approval_url"]


def test_async_act_scope_polling_returns_pending(setup):
    client, runtime, broker, token = setup
    # Submit
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "app.launch",
        "args": {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "package_name": "com.example.app",
        },
    })
    approval_id = res.json()["approval_id"]

    # Poll immediately — should be pending
    res = client.get(f"/agent/result/{approval_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["state"] == "pending"
    assert body["decision"] is None


def test_async_act_scope_approve_then_poll_completes(setup):
    client, runtime, broker, token = setup
    # Submit
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "app.launch",
        "args": {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "package_name": "com.example.app",
        },
    })
    approval_id = res.json()["approval_id"]

    # Approve via browser API
    res = client.post(f"/api/approve/{approval_id}")
    assert res.json()["decision"] == "approved"

    # Poll — should eventually be "completed" (worker thread needs a moment)
    deadline = time.monotonic() + 5.0
    final_body = None
    while time.monotonic() < deadline:
        res = client.get(f"/agent/result/{approval_id}")
        body = res.json()
        if body["state"] in ("completed", "error"):
            final_body = body
            break
        time.sleep(0.1)

    assert final_body is not None, "Polling timed out"
    assert final_body["state"] == "completed"
    assert final_body["decision"] == "approved"
    assert final_body["approver"] == "test-browser"
    assert final_body["result"]["launched"] is True


def test_async_act_scope_deny_then_poll_returns_denied(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "app.launch",
        "args": {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "package_name": "com.evil.app",
        },
    })
    approval_id = res.json()["approval_id"]

    # Deny
    client.post(f"/api/deny/{approval_id}")

    # Poll — should be denied
    deadline = time.monotonic() + 5.0
    final_body = None
    while time.monotonic() < deadline:
        res = client.get(f"/agent/result/{approval_id}")
        body = res.json()
        if body["state"] in ("denied", "error"):
            final_body = body
            break
        time.sleep(0.1)

    assert final_body is not None
    assert final_body["state"] in ("denied", "error")
    assert final_body["decision"] == "denied"


def test_async_result_unknown_id_returns_404(setup):
    client, *_ = setup
    res = client.get("/agent/result/does-not-exist")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Concurrent async calls — multiple pending at once
# ---------------------------------------------------------------------------


def test_async_multiple_pending_concurrent(setup):
    client, runtime, broker, token = setup

    approval_ids = []
    for i in range(3):
        res = client.post("/agent/call/async", json={
            "token": token.model_dump(mode="json"),
            "tool": "app.launch",
            "args": {
                "device_id": {"kind": "adb", "value": "mock-1"},
                "package_name": f"com.example.app{i}",
            },
        })
        assert res.status_code == 200
        approval_ids.append(res.json()["approval_id"])

    # All 3 should be pending
    pending = client.get("/api/pending").json()
    assert len(pending) == 3

    # Approve all 3
    for aid in approval_ids:
        client.post(f"/api/approve/{aid}")

    # All 3 should complete
    for aid in approval_ids:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            res = client.get(f"/agent/result/{aid}")
            body = res.json()
            if body["state"] == "completed":
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"approval {aid} did not complete")


# ---------------------------------------------------------------------------
# Invalid token
# ---------------------------------------------------------------------------


def test_async_invalid_token_returns_400(setup):
    client, *_ = setup
    res = client.post("/agent/call/async", json={
        "token": {"bad": "token"},
        "tool": "device.list",
        "args": {},
    })
    assert res.status_code == 400


def test_async_unknown_tool_returns_404(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "nonexistent.tool",
        "args": {},
    })
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Screenshot endpoint flow with async
# ---------------------------------------------------------------------------


def test_async_screenshot_returns_completed(setup):
    client, runtime, broker, token = setup
    res = client.post("/agent/call/async", json={
        "token": token.model_dump(mode="json"),
        "tool": "device.screenshot",
        "args": {"device_id": {"kind": "adb", "value": "mock-1"}},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "completed"
    assert "blob_ref" in body["result"]["screenshot"]
    blob_ref = body["result"]["screenshot"]["blob_ref"]

    # Fetch raw bytes
    res = client.get(f"/blob/{blob_ref}/raw")
    assert res.status_code == 200
    assert res.content.startswith(b"\x89PNG")
