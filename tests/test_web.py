"""Tests for the web server — pairing endpoints + agent API + session flow."""

from __future__ import annotations

import json
import time
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
    runtime = AdroidRuntime(
        issuer_id="adroid-test",
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path("/tmp/adroid-test-web2.auditlog"),
    )
    issuer = TokenIssuer(
        issuer_id=runtime.issuer_id,
        signing_key=runtime._token_private,  # noqa: SLF001
        public_key=runtime._token_public,  # noqa: SLF001
    )
    session_manager = SessionManager(issuer=issuer, max_sessions=20)

    # Bootstrap token (long-lived, NO grants — only used for pairing endpoint)
    from datetime import timedelta
    bootstrap_token = issuer.issue(
        subject="bootstrap",
        grants=[],
        ttl=timedelta(days=30),
    )

    app = create_app(
        runtime=runtime,
        session_manager=session_manager,
        issuer=issuer,
        bootstrap_token=bootstrap_token,
        browser_session_id="test-browser",
    )
    client = TestClient(app)
    return client, runtime, session_manager, bootstrap_token


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def test_ui_returns_html(setup):
    client, *_ = setup
    res = client.get("/ui")
    assert res.status_code == 200
    assert "Adroid" in res.text
    assert "Pairing" in res.text


def test_api_state_initial(setup):
    client, *_ = setup
    res = client.get("/api/state")
    assert res.status_code == 200
    body = res.json()
    assert body["pairings"] == []
    assert body["sessions"] == []
    assert "terminal" in body
    assert body["issuer_id"] == "adroid-test"


def test_api_tools_lists_all(setup):
    client, *_ = setup
    res = client.get("/api/tools")
    assert res.status_code == 200
    names = {t["name"] for t in res.json()}
    assert "device.list" in names
    assert "app.launch" in names


# ---------------------------------------------------------------------------
# Pairing flow (AI side)
# ---------------------------------------------------------------------------


def test_agent_session_request_succeeds(setup):
    client, runtime, sm, bootstrap = setup
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap.model_dump(mode="json"),
        "agent_id": "agent:test",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["pairing_id"]
    assert body["pairing_url"]
    assert body["pairing_id"] in body["pairing_url"]
    assert "/ui#pairing-" in body["pairing_url"]


def test_agent_session_request_rejects_bad_bootstrap(setup):
    client, *_ = setup
    res = client.post("/agent/session/request", json={
        "bootstrap_token": {"not": "a token"},
        "agent_id": "agent:test",
    })
    assert res.status_code == 401


def test_agent_session_request_rejects_wrong_bootstrap(setup):
    """Bootstrap token from a different runtime must be rejected."""
    client, runtime, *_ = setup
    # Generate a different keypair and issue a token
    from adroid.permissions.tokens import TokenIssuer, generate_signing_keypair
    from datetime import timedelta
    other_priv, other_pub = generate_signing_keypair()
    other_issuer = TokenIssuer(issuer_id="other", signing_key=other_priv, public_key=other_pub)
    other_token = other_issuer.issue(subject="bootstrap", grants=[], ttl=timedelta(days=1))

    res = client.post("/agent/session/request", json={
        "bootstrap_token": other_token.model_dump(mode="json"),
        "agent_id": "agent:test",
    })
    assert res.status_code == 401


def test_agent_session_status_pending(setup):
    client, runtime, sm, bootstrap = setup
    # Request pairing
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap.model_dump(mode="json"),
        "agent_id": "agent:test",
    })
    pairing_id = res.json()["pairing_id"]

    # Poll immediately — should be pending
    res = client.get(f"/agent/session/{pairing_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "pending"
    assert body["session_token"] is None


def test_agent_session_status_approved_returns_token(setup):
    client, runtime, sm, bootstrap = setup
    # Request pairing
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap.model_dump(mode="json"),
        "agent_id": "agent:test",
    })
    pairing_id = res.json()["pairing_id"]

    # Approve via browser API
    res = client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": 3600})
    assert res.status_code == 200

    # Poll — should be approved + have session_token
    res = client.get(f"/agent/session/{pairing_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "approved"
    assert body["session_token"] is not None
    assert body["session_token"]["subject"] == "session:agent:test"
    assert "signature" in body["session_token"]


def test_agent_session_status_denied(setup):
    client, runtime, sm, bootstrap = setup
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap.model_dump(mode="json"),
        "agent_id": "agent:test",
    })
    pairing_id = res.json()["pairing_id"]
    client.post(f"/api/pair/{pairing_id}/deny")

    res = client.get(f"/agent/session/{pairing_id}")
    body = res.json()
    assert body["status"] == "denied"
    assert body["session_token"] is None


def test_agent_session_status_unknown_returns_404(setup):
    client, *_ = setup
    res = client.get("/agent/session/does-not-exist")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# Browser approve/deny endpoints
# ---------------------------------------------------------------------------


def test_approve_with_invalid_ttl_returns_400(setup):
    client, runtime, sm, bootstrap = setup
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap.model_dump(mode="json"),
        "agent_id": "agent:test",
    })
    pairing_id = res.json()["pairing_id"]

    # TTL too short
    res = client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": 30})
    assert res.status_code == 400


def test_approve_unknown_pairing_returns_400(setup):
    client, *_ = setup
    res = client.post("/api/pair/bogus/approve", json={"ttl_seconds": 3600})
    assert res.status_code == 400


def test_approve_already_decided_returns_400(setup):
    client, runtime, sm, bootstrap = setup
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap.model_dump(mode="json"),
        "agent_id": "agent:test",
    })
    pairing_id = res.json()["pairing_id"]
    client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": 3600})

    res = client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": 7200})
    assert res.status_code == 400


# ---------------------------------------------------------------------------
# Agent call flow (post-pairing — no per-action approval!)
# ---------------------------------------------------------------------------


def _pair(client, bootstrap, agent_id="agent:test", ttl=3600):
    """Helper: request pairing + approve + return session token."""
    res = client.post("/agent/session/request", json={
        "bootstrap_token": bootstrap.model_dump(mode="json"),
        "agent_id": agent_id,
    })
    pairing_id = res.json()["pairing_id"]
    client.post(f"/api/pair/{pairing_id}/approve", json={"ttl_seconds": ttl})
    res = client.get(f"/agent/session/{pairing_id}")
    return res.json()["session_token"]


def test_agent_call_after_pairing_succeeds(setup):
    client, runtime, sm, bootstrap = setup
    token = _pair(client, bootstrap)

    res = client.post("/agent/call", json={
        "token": token,
        "tool": "device.list",
        "args": {},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert len(body["result"]["devices"]) == 1


def test_agent_call_screenshot(setup):
    client, *_ = setup
    token = _pair(client, _get_bootstrap(setup))
    res = client.post("/agent/call", json={
        "token": token,
        "tool": "device.screenshot",
        "args": {"device_id": {"kind": "adb", "value": "mock-1"}},
    })
    body = res.json()
    assert body["ok"] is True
    blob_ref = body["result"]["screenshot"]["blob_ref"]

    # Fetch raw bytes
    res = client.get(f"/blob/{blob_ref}/raw")
    assert res.status_code == 200
    assert res.content.startswith(b"\x89PNG")


def _get_bootstrap(setup):
    return setup[3]


def test_agent_call_app_launch_no_per_action_approval(setup):
    """KEY TEST: app.launch (ACT scope) goes through WITHOUT interactive
    approval. The session token already has full access."""
    client, *_ = setup
    token = _pair(client, _get_bootstrap(setup))

    res = client.post("/agent/call", json={
        "token": token,
        "tool": "app.launch",
        "args": {
            "device_id": {"kind": "adb", "value": "mock-1"},
            "package_name": "com.example.app",
        },
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["result"]["launched"] is True


def test_agent_call_after_disconnect_rejected(setup):
    """After user clicks Disconnect, the session token is revoked."""
    client, runtime, sm, bootstrap = setup
    token = _pair(client, bootstrap)
    session_id = token["token_id"]

    # Disconnect via browser
    res = client.post(f"/api/session/{session_id}/disconnect")
    assert res.status_code == 200

    # Now agent call should be rejected
    res = client.post("/agent/call", json={
        "token": token,
        "tool": "device.list",
        "args": {},
    })
    body = res.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "adroid.session.revoked"


def test_agent_call_with_unpaired_token_rejected(setup):
    """A token that was NOT issued via pairing (e.g. minted directly) should
    fail the gate's standard checks (issuer mismatch / no grants / etc.)."""
    client, runtime, *_ = setup
    # Mint a token with no grants at all
    from datetime import timedelta
    from adroid.permissions.tokens import TokenIssuer
    # We can't easily get a "wrong" token here — the runtime's own issuer is
    # what validates. So let's test with a token that has no capabilities.
    # Use the runtime's issuer directly to mint a no-grants token.
    issuer = TokenIssuer(
        issuer_id=runtime.issuer_id,
        signing_key=runtime._token_private,  # noqa: SLF001
        public_key=runtime._token_public,  # noqa: SLF001
    )
    bad_token = issuer.issue(subject="bad", grants=[], ttl=timedelta(hours=1))

    res = client.post("/agent/call", json={
        "token": bad_token.model_dump(mode="json"),
        "tool": "device.list",
        "args": {},
    })
    body = res.json()
    assert body["ok"] is False


# ---------------------------------------------------------------------------
# Multiple AI sessions
# ---------------------------------------------------------------------------


def test_multiple_sessions_concurrent(setup):
    """Multiple AI agents can pair + call concurrently."""
    client, *_ = setup
    bootstrap = _get_bootstrap(setup)

    tokens = []
    for i in range(3):
        t = _pair(client, bootstrap, agent_id=f"agent:{i}")
        tokens.append(t)

    # All three can call
    for t in tokens:
        res = client.post("/agent/call", json={
            "token": t, "tool": "device.list", "args": {},
        })
        assert res.json()["ok"] is True


def test_disconnect_one_does_not_affect_others(setup):
    client, *_ = setup
    bootstrap = _get_bootstrap(setup)
    t1 = _pair(client, bootstrap, agent_id="a1")
    t2 = _pair(client, bootstrap, agent_id="a2")

    # Disconnect t1
    client.post(f"/api/session/{t1['token_id']}/disconnect")

    # t1 should be revoked
    res = client.post("/agent/call", json={"token": t1, "tool": "device.list", "args": {}})
    assert res.json()["ok"] is False

    # t2 should still work
    res = client.post("/agent/call", json={"token": t2, "tool": "device.list", "args": {}})
    assert res.json()["ok"] is True


# ---------------------------------------------------------------------------
# Audit + terminal
# ---------------------------------------------------------------------------


def test_audit_log_records_calls(setup):
    client, *_ = setup
    token = _pair(client, _get_bootstrap(setup))
    client.post("/agent/call", json={"token": token, "tool": "device.list", "args": {}})

    res = client.get("/api/audit")
    events = res.json()
    assert any(e["method"] == "runtime.device_list" for e in events)


def test_terminal_stream_records_pairing(setup):
    client, *_ = setup
    _pair(client, _get_bootstrap(setup))
    res = client.get("/api/state")
    state = res.json()
    # Terminal should have pairing-related lines
    pairing_lines = [l for l in state["terminal"] if l["kind"] == "pairing"]
    assert len(pairing_lines) >= 2  # requested + approved
