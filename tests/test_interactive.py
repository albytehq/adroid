"""Tests for the interactive permission gate + approval broker."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest

from adroid.contract.errors import PermissionDeniedError
from adroid.contract.types import Capability, CapabilityGrant, Scope
from adroid.permissions.gate import RateLimiter
from adroid.permissions.interactive import ApprovalBroker, InteractiveGate, PendingApproval
from adroid.permissions.tokens import TokenIssuer, generate_signing_keypair


@pytest.fixture
def gate_and_broker():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    broker = ApprovalBroker()
    gate = InteractiveGate(
        issuer_id="adroid-test",
        public_key=public,
        broker=broker,
        approval_timeout_seconds=2.0,  # short for tests
        rate_limiter=RateLimiter(),
    )
    return gate, broker, issuer


# ---------------------------------------------------------------------------
# ApprovalBroker — basic mechanics
# ---------------------------------------------------------------------------


def test_broker_request_blocks_until_resolved():
    broker = ApprovalBroker()

    approval = PendingApproval(
        token_id=None,  # type: ignore[arg-type]
        subject="agent:test",
        capability=Capability.APP_LAUNCH,
        required_scope=Scope.ACT,
        method="runtime.app_launch",
        args={"x": 1},
    )

    result = {}

    def requester():
        result["decided"] = broker.request(approval, timeout_seconds=2.0)

    t = threading.Thread(target=requester)
    t.start()

    # Give the requester a moment to register
    time.sleep(0.1)

    # Should be one pending
    pending = broker.pending()
    assert len(pending) == 1
    assert pending[0].approval_id == approval.approval_id

    # Resolve
    ok = broker.resolve(approval.approval_id, decision="approved", approver="browser-1")
    assert ok is True

    t.join(timeout=2.0)

    decided = result["decided"]
    assert decided.decision == "approved"
    assert decided.approver == "browser-1"
    assert decided.decided_at is not None


def test_broker_request_times_out():
    broker = ApprovalBroker()
    approval = PendingApproval(
        token_id=None,  # type: ignore[arg-type]
        subject="agent:test",
        capability=Capability.APP_LAUNCH,
        required_scope=Scope.ACT,
        method="runtime.app_launch",
        args={},
    )

    start = time.monotonic()
    decided = broker.request(approval, timeout_seconds=0.3)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.3
    assert decided.decision == "timeout"


def test_broker_resolve_returns_false_for_unknown_id():
    broker = ApprovalBroker()
    assert broker.resolve("does-not-exist", decision="approved") is False


def test_broker_history_capped():
    """History grows via broker.request() (which appends decided approvals).
    We exhaust the cap by issuing many fast-timing-out requests."""
    broker = ApprovalBroker()
    broker._history_cap = 5  # noqa: SLF001
    for i in range(10):
        a = PendingApproval(
            token_id=None,  # type: ignore[arg-type]
            subject=f"agent:{i}",
            capability=Capability.APP_LAUNCH,
            required_scope=Scope.ACT,
            method="x",
            args={},
        )
        # request() with 0 timeout returns immediately and appends to history
        broker.request(a, timeout_seconds=0.01)
    assert len(broker.history()) == 5


# ---------------------------------------------------------------------------
# InteractiveGate — integration with broker
# ---------------------------------------------------------------------------


def test_interactive_gate_read_scope_skips_approval(gate_and_broker):
    gate, broker, issuer = gate_and_broker
    token = issuer.issue(
        subject="agent:r",
        grants=[(Capability.DEVICE_LIST, Scope.READ)],
    )

    # READ scope — should pass immediately, no approval needed
    approval = gate.authorize_interactive(
        token,
        Capability.DEVICE_LIST,
        Scope.READ,
        method="runtime.device_list",
        args={},
    )
    assert approval is None  # no approval object means auto-approved


def test_interactive_gate_act_scope_requires_approval(gate_and_broker):
    gate, broker, issuer = gate_and_broker
    token = issuer.issue(
        subject="agent:a",
        grants=[(Capability.APP_LAUNCH, Scope.ACT)],
    )

    result = {}

    def requester():
        try:
            approval = gate.authorize_interactive(
                token,
                Capability.APP_LAUNCH,
                Scope.ACT,
                method="runtime.app_launch",
                args={"package_name": "com.example.app"},
            )
            result["approval"] = approval
        except Exception as exc:
            result["error"] = exc

    t = threading.Thread(target=requester)
    t.start()

    # Wait for pending to appear
    for _ in range(20):
        if broker.pending():
            break
        time.sleep(0.05)

    pending = broker.pending()
    assert len(pending) == 1
    assert pending[0].capability == Capability.APP_LAUNCH

    # Approve
    broker.resolve(pending[0].approval_id, decision="approved", approver="browser-1")

    t.join(timeout=2.0)

    assert "error" not in result
    assert result["approval"].decision == "approved"
    assert result["approval"].approver == "browser-1"


def test_interactive_gate_denial_raises(gate_and_broker):
    gate, broker, issuer = gate_and_broker
    token = issuer.issue(
        subject="agent:a",
        grants=[(Capability.APP_LAUNCH, Scope.ACT)],
    )

    result = {}

    def requester():
        try:
            gate.authorize_interactive(
                token,
                Capability.APP_LAUNCH,
                Scope.ACT,
                method="runtime.app_launch",
                args={},
            )
            result["ok"] = True
        except PermissionDeniedError as exc:
            result["denied"] = exc

    t = threading.Thread(target=requester)
    t.start()

    for _ in range(20):
        if broker.pending():
            break
        time.sleep(0.05)

    pending = broker.pending()
    broker.resolve(pending[0].approval_id, decision="denied", approver="browser-1")

    t.join(timeout=2.0)

    assert "denied" in result
    assert result["denied"].code == "adroid.permission.interactive_denied"


def test_interactive_gate_timeout_raises(gate_and_broker):
    gate, broker, issuer = gate_and_broker
    token = issuer.issue(
        subject="agent:a",
        grants=[(Capability.APP_LAUNCH, Scope.ACT)],
    )

    with pytest.raises(PermissionDeniedError) as exc:
        gate.authorize_interactive(
            token,
            Capability.APP_LAUNCH,
            Scope.ACT,
            method="runtime.app_launch",
            args={},
        )
    assert exc.value.code == "adroid.permission.interactive_timeout"


def test_interactive_gate_token_invalid_still_raises_first(gate_and_broker):
    """Token check happens before approval request — bad token = no pending."""
    gate, broker, issuer = gate_and_broker
    # Different issuer keypair entirely
    other_priv, other_pub = generate_signing_keypair()
    other_issuer = TokenIssuer(issuer_id="other", signing_key=other_priv, public_key=other_pub)
    bad_token = other_issuer.issue(
        subject="agent:evil",
        grants=[(Capability.APP_LAUNCH, Scope.ACT)],
    )

    with pytest.raises(PermissionDeniedError):
        gate.authorize_interactive(
            bad_token,
            Capability.APP_LAUNCH,
            Scope.ACT,
            method="runtime.app_launch",
            args={},
        )

    # Should NOT have created a pending approval
    assert broker.pending() == []


def test_interactive_gate_require_for_read_flag():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    broker = ApprovalBroker()
    gate = InteractiveGate(
        issuer_id="adroid-test",
        public_key=public,
        broker=broker,
        approval_timeout_seconds=0.3,
        require_approval_for_read=True,
    )

    token = issuer.issue(
        subject="agent:r",
        grants=[(Capability.DEVICE_LIST, Scope.READ)],
    )

    # With require_approval_for_read=True, even READ blocks + times out
    with pytest.raises(PermissionDeniedError) as exc:
        gate.authorize_interactive(
            token,
            Capability.DEVICE_LIST,
            Scope.READ,
            method="runtime.device_list",
            args={},
        )
    assert exc.value.code == "adroid.permission.interactive_timeout"
