"""Tests for the session manager — pairing flow, TTL, revocation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adroid.contract.errors import AdroidError, RateLimitedError
from adroid.permissions.sessions import SessionManager
from adroid.permissions.tokens import TokenIssuer, generate_signing_keypair


@pytest.fixture
def manager():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    return SessionManager(issuer=issuer, max_sessions=5, max_pairing_wait_seconds=1)


# ---------------------------------------------------------------------------
# Pairing request
# ---------------------------------------------------------------------------


def test_request_pairing_creates_pending(manager):
    p = manager.request_pairing(agent_id="agent:foo")
    assert p.status == "pending"
    assert p.agent_id == "agent:foo"
    assert p.pairing_id  # UUID-generated


def test_request_pairing_bad_agent_id_rejected(manager):
    with pytest.raises(AdroidError) as exc:
        manager.request_pairing(agent_id="")
    assert exc.value.code == "adroid.session.bad_agent_id"


def test_list_pending_pairings_excludes_decided(manager):
    p1 = manager.request_pairing(agent_id="a1")
    p2 = manager.request_pairing(agent_id="a2")
    pending = manager.list_pending_pairings()
    assert len(pending) == 2

    manager.deny_pairing(pairing_id=p1.pairing_id, approver="browser-1")
    pending = manager.list_pending_pairings()
    assert len(pending) == 1
    assert pending[0].pairing_id == p2.pairing_id


def test_stale_pairings_expired(manager):
    """Pairings older than max_pairing_wait_seconds get marked expired."""
    p = manager.request_pairing(agent_id="a")
    # Wait beyond max_pairing_wait (1 second in fixture)
    import time
    time.sleep(1.1)
    pending = manager.list_pending_pairings()
    assert len(pending) == 0  # expired ones are not listed
    refreshed = manager.get_pairing(p.pairing_id)
    assert refreshed.status == "expired"


# ---------------------------------------------------------------------------
# Approve / deny
# ---------------------------------------------------------------------------


def test_approve_pairing_mints_session_token(manager):
    p = manager.request_pairing(agent_id="a")
    decided = manager.approve_pairing(
        pairing_id=p.pairing_id,
        ttl_seconds=3600,
        approver="browser-1",
    )
    assert decided.status == "approved"
    assert decided.ttl_seconds == 3600
    assert decided.decided_by == "browser-1"
    assert decided.session_token_json  # the CapabilityToken JSON
    assert decided.session_id  # the token_id

    # Session should be in the active list
    sessions = manager.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].agent_id == "a"
    assert sessions[0].is_active


def test_approve_pairing_bad_ttl_rejected(manager):
    p = manager.request_pairing(agent_id="a")
    with pytest.raises(AdroidError) as exc:
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=30, approver="b")
    assert exc.value.code == "adroid.session.bad_ttl"

    with pytest.raises(AdroidError):
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=10*365*86400, approver="b")


def test_deny_pairing(manager):
    p = manager.request_pairing(agent_id="a")
    decided = manager.deny_pairing(pairing_id=p.pairing_id, approver="b")
    assert decided.status == "denied"
    assert decided.decided_by == "b"
    # No session created
    assert manager.list_sessions() == []


def test_approve_unknown_pairing_rejected(manager):
    with pytest.raises(AdroidError) as exc:
        manager.approve_pairing(pairing_id="bogus", ttl_seconds=3600, approver="b")
    assert exc.value.code == "adroid.session.unknown_pairing"


def test_approve_already_decided_rejected(manager):
    p = manager.request_pairing(agent_id="a")
    manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
    with pytest.raises(AdroidError) as exc:
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=7200, approver="b")
    assert exc.value.code == "adroid.session.pairing_already_decided"


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


def test_disconnect_session_revokes(manager):
    p = manager.request_pairing(agent_id="a")
    manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
    session_id = p.session_id

    ok = manager.disconnect_session(session_id)
    assert ok is True

    sessions = manager.list_sessions()
    assert len(sessions) == 0  # disconnected sessions excluded from active list

    # Token ID is on the revocation list
    from uuid import UUID
    # session_id is the str(token_id); convert to UUID for is_revoked
    token_id = UUID(session_id)
    assert manager.is_revoked(token_id) is True


def test_disconnect_unknown_session_returns_false(manager):
    assert manager.disconnect_session("bogus") is False


def test_disconnect_already_revoked_returns_false(manager):
    p = manager.request_pairing(agent_id="a")
    manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
    assert manager.disconnect_session(p.session_id) is True
    assert manager.disconnect_session(p.session_id) is False


def test_max_sessions_enforced(manager):
    """max_sessions=5 in fixture. Requesting a 6th should rate-limit."""
    for i in range(5):
        p = manager.request_pairing(agent_id=f"a{i}")
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")

    with pytest.raises(RateLimitedError) as exc:
        manager.request_pairing(agent_id="a6")
    assert exc.value.code == "adroid.session.max_sessions"


def test_max_sessions_frees_up_after_disconnect(manager):
    for i in range(5):
        p = manager.request_pairing(agent_id=f"a{i}")
        manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")

    # Disconnect one
    sessions = manager.list_sessions()
    manager.disconnect_session(sessions[0].session_id)

    # Now we can request a new one
    p = manager.request_pairing(agent_id="a6")
    assert p.status == "pending"


def test_cleanup_expired_marks_sessions_revoked(manager):
    p = manager.request_pairing(agent_id="a")
    # Approve with a very short TTL
    decided = manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=60, approver="b")
    # Manually expire the session by editing its expires_at
    with manager._lock:  # noqa: SLF001
        s = manager._sessions[p.session_id]  # noqa: SLF001
        s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    removed = manager.cleanup_expired()
    assert removed == 1

    from uuid import UUID
    assert manager.is_revoked(UUID(p.session_id)) is True


def test_get_session_by_token(manager):
    p = manager.request_pairing(agent_id="a")
    manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
    from uuid import UUID
    token_id = UUID(p.session_id)
    s = manager.get_session_by_token(token_id)
    assert s is not None
    assert s.agent_id == "a"


# ---------------------------------------------------------------------------
# v0.3.7: cleanup_expired should prune session dicts (memory leak fix)
# ---------------------------------------------------------------------------


def test_cleanup_expired_prunes_session_dicts(manager):
    """cleanup_expired should remove Session objects from _sessions.

    Previously it only flipped revoked=True — leaving the Session object
    in the dict forever, causing O(n) scans on every request_pairing
    and unbounded memory growth in long-running runtimes.
    """
    from datetime import datetime, timedelta, timezone
    from uuid import UUID
    p = manager.request_pairing(agent_id="a")
    manager.approve_pairing(pairing_id=p.pairing_id, ttl_seconds=3600, approver="b")
    # session_id == token_id (hex string), per sessions.py:78
    session_id = p.session_id
    token_id = UUID(session_id)
    # Pre-check: session is in the dict
    assert session_id in manager._sessions
    assert token_id in manager._token_to_session
    # Manually expire it
    with manager._lock:
        s = manager._sessions[session_id]
        s.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    removed = manager.cleanup_expired()
    assert removed == 1
    # Post-check: Session object pruned from dict
    assert session_id not in manager._sessions
    assert token_id not in manager._token_to_session
    # But the token_id is still in the revoked set so is_revoked returns True
    assert manager.is_revoked(token_id) is True
