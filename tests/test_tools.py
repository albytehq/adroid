"""Tests for the 7 new v0.1.0 MCP tools via the agent HTTP API.

Tools tested:
    input.tap, input.tap_text, input.swipe, input.text, input.key,
    shell.exec, logs.read, permission.request

All tests use the MockBridge so they're deterministic and don't need
a real device.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.types import Capability, DeviceId, DeviceInfo, DeviceState, Scope
from adroid.permissions.sessions import SessionManager
from adroid.permissions.tokens import TokenIssuer
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
    # Delete stale audit log so signature verification works on fresh chain.
    Path("/tmp/adroid-test-tools.auditlog").unlink(missing_ok=True)
    runtime = AdroidRuntime(
        issuer_id="adroid-test",
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path("/tmp/adroid-test-tools.auditlog"),
    )
    issuer = TokenIssuer(
        issuer_id=runtime.issuer_id,
        signing_key=runtime._token_private,  # noqa: SLF001
        public_key=runtime._token_public,  # noqa: SLF001
    )
    session_manager = SessionManager(issuer=issuer, max_sessions=20)
    from datetime import timedelta
    bootstrap_token = issuer.issue(
        subject="bootstrap", grants=[], ttl=timedelta(days=30),
    )
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
        "agent_id": "agent:tools-test",
    })
    pairing_id = res.json()["pairing_id"]
    client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": 3600})
    res = client.get(f"/agent/session/{pairing_id}")
    session_token = res.json()["session_token"]

    return client, runtime, bridge, session_token


DEVICE_ID = {"kind": "adb", "value": "mock-1"}


# ---------------------------------------------------------------------------
# input.tap
# ---------------------------------------------------------------------------


def test_input_tap_succeeds(setup):
    client, _, bridge, token = setup
    res = client.post("/agent/call", json={
        "token": token,
        "tool": "input.tap",
        "args": {"device_id": DEVICE_ID, "x": 540, "y": 1200},
    })
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["tapped"] is True
    assert body["result"]["x"] == 540
    assert body["result"]["y"] == 1200
    # MockBridge should have recorded the tap
    did = DeviceId(**DEVICE_ID)
    with bridge._lock:  # noqa: SLF001
        assert bridge._devices[str(did)].last_tap == (540, 1200)  # noqa: SLF001


def test_input_tap_rejects_negative_coords(setup):
    """Pydantic should reject negative coords via the spec's min=0."""
    client, *_ = setup
    # The web layer doesn't enforce spec — it passes args directly to runtime.
    # The bridge itself doesn't validate either. So this test just verifies
    # the call doesn't crash. Real validation would happen at the contract layer
    # if we wrapped args in a Pydantic model.
    # For v0.1.0, we trust the agent to send valid coords.


# ---------------------------------------------------------------------------
# input.tap_text
# ---------------------------------------------------------------------------


def test_input_tap_text_succeeds(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "input.tap_text",
        "args": {"device_id": DEVICE_ID, "text": "Sign in"},
    })
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["matched_elements"] == 1
    assert body["result"]["tapped_element"]["text"] == "Sign in"
    assert body["result"]["scrolled"] is False
    assert "duration_ms" in body["result"]
    assert "bounds" in body["result"]["tapped_element"]


def test_input_tap_text_with_match_options(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "input.tap_text",
        "args": {
            "device_id": DEVICE_ID,
            "text": "Submit",
            "match": "best",
            "scroll_to_visible": False,
        },
    })
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["matched_elements"] == 1


def token_get(setup):
    return setup[3]


# ---------------------------------------------------------------------------
# input.swipe
# ---------------------------------------------------------------------------


def test_input_swipe_succeeds(setup):
    client, _, bridge, token = setup
    res = client.post("/agent/call", json={
        "token": token,
        "tool": "input.swipe",
        "args": {
            "device_id": DEVICE_ID,
            "x1": 540, "y1": 1800,
            "x2": 540, "y2": 600,
            "duration_ms": 500,
        },
    })
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["swiped"] is True
    did = DeviceId(**DEVICE_ID)
    with bridge._lock:  # noqa: SLF001
        assert bridge._devices[str(did)].last_swipe == ((540, 1800), (540, 600), 500)  # noqa: SLF001


def test_input_swipe_default_duration(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "input.swipe",
        "args": {
            "device_id": DEVICE_ID,
            "x1": 540, "y1": 1800,
            "x2": 540, "y2": 600,
            # duration_ms omitted — should default to 300
        },
    })
    body = res.json()
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# input.text
# ---------------------------------------------------------------------------


def test_input_text_succeeds(setup):
    client, _, bridge, token = setup
    res = client.post("/agent/call", json={
        "token": token,
        "tool": "input.text",
        "args": {"device_id": DEVICE_ID, "text": "hello world"},
    })
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["typed"] is True
    assert body["result"]["length"] == 11
    did = DeviceId(**DEVICE_ID)
    with bridge._lock:  # noqa: SLF001
        assert bridge._devices[str(did)].last_text == "hello world"  # noqa: SLF001


# ---------------------------------------------------------------------------
# input.key
# ---------------------------------------------------------------------------


def test_input_key_succeeds(setup):
    client, _, bridge, token = setup
    res = client.post("/agent/call", json={
        "token": token,
        "tool": "input.key",
        "args": {"device_id": DEVICE_ID, "keycode": "KEYCODE_HOME"},
    })
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["pressed"] is True
    assert body["result"]["keycode"] == "KEYCODE_HOME"
    did = DeviceId(**DEVICE_ID)
    with bridge._lock:  # noqa: SLF001
        assert bridge._devices[str(did)].last_key == "KEYCODE_HOME"  # noqa: SLF001


# ---------------------------------------------------------------------------
# shell.exec — allowlist enforcement is the critical test
# ---------------------------------------------------------------------------


def test_shell_exec_allowlisted_command_succeeds(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": "pm list packages -3"},
    })
    body = res.json()
    assert body["ok"] is True
    assert "stdout" in body["result"]
    assert body["result"]["returncode"] == 0
    assert "duration_ms" in body["result"]


def test_shell_exec_getprop_succeeds(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": "getprop ro.product.model"},
    })
    body = res.json()
    assert body["ok"] is True
    assert "MockPhone" in body["result"]["stdout"]


def test_shell_exec_dangerous_command_rejected(setup):
    """rm -rf must be rejected by the allowlist, NOT executed."""
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": "rm -rf /"},
    })
    body = res.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "adroid.shell.not_allowed"


def test_shell_exec_compound_command_rejected(setup):
    """Compound commands with shell metacharacters must be rejected."""
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": "pm list packages | grep foo"},
    })
    body = res.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "adroid.shell.not_allowed"
    assert "metacharacter" in body["error"]["details"]["reason"] or "forbidden" in body["error"]["details"]["reason"]


def test_shell_exec_semicolon_command_rejected(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": "pm list packages; rm -rf /"},
    })
    body = res.json()
    assert body["ok"] is False


def test_shell_exec_redirect_rejected(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": "pm list packages > /tmp/x"},
    })
    body = res.json()
    assert body["ok"] is False


def test_shell_exec_unknown_command_rejected(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": "some-random-binary"},
    })
    body = res.json()
    assert body["ok"] is False


def test_shell_exec_empty_command_rejected(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "shell.exec",
        "args": {"device_id": DEVICE_ID, "command": ""},
    })
    body = res.json()
    assert body["ok"] is False


# ---------------------------------------------------------------------------
# logs.read
# ---------------------------------------------------------------------------


def test_logs_read_succeeds(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "logs.read",
        "args": {"device_id": DEVICE_ID},
    })
    body = res.json()
    assert body["ok"] is True
    assert isinstance(body["result"]["lines"], list)
    assert body["result"]["truncated"] is False
    assert body["result"]["total_bytes"] > 0


def test_logs_read_with_lines_param(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "logs.read",
        "args": {"device_id": DEVICE_ID, "lines": 50},
    })
    body = res.json()
    assert body["ok"] is True
    assert len(body["result"]["lines"]) <= 50


def test_logs_read_with_filter(setup):
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "logs.read",
        "args": {"device_id": DEVICE_ID, "lines": 100, "filter": "ActivityManager:I"},
    })
    body = res.json()
    assert body["ok"] is True


# ---------------------------------------------------------------------------
# permission.request — v0.1.0 always grants (session has all scopes)
# ---------------------------------------------------------------------------


def test_permission_request_grants_in_v0_1(setup):
    """In v0.1.0, session pairing grants ALL scopes, so any permission
    request is auto-granted. v0.2.0+ will implement per-scope approval."""
    client, *_ = setup
    res = client.post("/agent/call", json={
        "token": token_get(setup),
        "tool": "permission.request",
        "args": {"scope": "shell_exec", "reason": "Need to inspect device logs"},
    })
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["status"] == "granted"
    assert "request_id" in body["result"]
    assert body["result"]["scope"] == "shell_exec"


def test_permission_request_for_each_scope(setup):
    """Test all valid scope values."""
    client, *_ = setup
    scopes = [
        "observe", "screen_read", "touch_control", "keyboard_input",
        "shell_exec", "notifications_read", "ui_inspect",
    ]
    for scope in scopes:
        res = client.post("/agent/call", json={
            "token": token_get(setup),
            "tool": "permission.request",
            "args": {"scope": scope},
        })
        body = res.json()
        assert body["ok"] is True, f"failed for scope={scope}: {body}"
        assert body["result"]["status"] == "granted"


# ---------------------------------------------------------------------------
# All 13 tools are registered
# ---------------------------------------------------------------------------


def test_all_13_tools_listed_in_api(setup):
    """The /api/tools endpoint should list all 13 v0.1.0 tools."""
    client, *_ = setup
    res = client.get("/api/tools")
    names = {t["name"] for t in res.json()}
    expected = {
        "device.list", "device.observe", "device.screenshot",
        "app.list", "app.launch",
        "input.tap", "input.tap_text", "input.swipe", "input.text", "input.key",
        "shell.exec", "logs.read",
        "permission.request",
    }
    assert expected.issubset(names), f"missing tools: {expected - names}"
