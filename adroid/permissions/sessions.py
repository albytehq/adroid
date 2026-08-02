"""Session-based pairing: one-time approval → full access until expiry/disconnect.

This replaces the per-action InteractiveGate. The flow is:

    1. AI agent POSTs /agent/session/request with a bootstrap token.
       → Server creates a PairingRequest, returns pairing_id + pairing_url.
    2. AI shares the pairing_url with the human (chat, email, whatever).
    3. Human opens the URL in browser → dashboard shows the pairing request.
    4. Human picks a TTL (1h, 6h, 24h, 7d, custom) and taps **Approve**.
       → Server mints a session CapabilityToken with ALL capabilities at ACT
         scope, returns it to the AI (via polling).
    5. AI now has full access — calls any tool without further approval.
       Every call is still audited and streamed to the dashboard terminal.
    6. Session ends when:
       a. TTL expires (token's expires_at fires — gate rejects)
       b. Human clicks "Disconnect" in dashboard (token_id added to revoke list)
       c. Runtime restarts (in-memory state lost — user re-pairs next time)

v0.1.0 limits: 1 HP per runtime, max 20 concurrent AI sessions per runtime.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable
from uuid import UUID

from adroid.contract.errors import AdroidError, PermissionDeniedError, RateLimitedError
from adroid.contract.types import Capability, CapabilityToken, Scope
from adroid.permissions.tokens import TokenIssuer


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class PairingRequest:
    """A pending request from an AI agent to pair with this runtime.

    The browser UI renders this; the user picks TTL and approves (or denies).
    """

    agent_id: str  # who's requesting (set by AI in /agent/session/request)
    pairing_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # "pending" | "approved" | "denied" | "expired"
    status: str = "pending"
    decided_at: datetime | None = None
    decided_by: str | None = None  # browser session ID of approver
    # Filled when approved:
    ttl_seconds: int | None = None
    session_token_json: str | None = None  # the CapabilityToken JSON to give back to AI
    session_id: str | None = None  # == CapabilityToken.token_id

    def to_dict(self) -> dict:
        return {
            "pairing_id": self.pairing_id,
            "agent_id": self.agent_id,
            "requested_at": self.requested_at.isoformat(),
            "status": self.status,
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decided_by": self.decided_by,
            "ttl_seconds": self.ttl_seconds,
            "session_id": self.session_id,
            # session_token_json is NOT exposed here — only the AI polling
            # /agent/session/{pairing_id} gets it, after approval.
        }


@dataclass
class Session:
    """An active AI session. Created when a pairing is approved."""

    session_id: str  # == CapabilityToken.token_id (hex)
    token_id: UUID
    agent_id: str
    created_at: datetime
    expires_at: datetime  # user-set TTL
    pairing_id: str
    revoked: bool = False
    revoked_at: datetime | None = None

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at

    @property
    def is_active(self) -> bool:
        return not self.revoked and not self.is_expired

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "revoked": self.revoked,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "pairing_id": self.pairing_id,
            "is_active": self.is_active,
            "remaining_seconds": max(
                0, int((self.expires_at - datetime.now(timezone.utc)).total_seconds())
            ) if not self.revoked else 0,
        }


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SessionManager:
    """Thread-safe pairing + session registry.

    One instance per runtime. The web layer shares it with the gate so
    revocation is enforced on every tool call.
    """

    def __init__(
        self,
        *,
        issuer: TokenIssuer,
        max_sessions: int = 20,
        max_pairing_wait_seconds: int = 600,  # 10 min for user to approve
    ):
        self._issuer = issuer
        self._max_sessions = max_sessions
        self._max_pairing_wait = max_pairing_wait_seconds
        self._pairings: dict[str, PairingRequest] = {}
        self._sessions: dict[str, Session] = {}  # session_id → Session
        self._token_to_session: dict[UUID, Session] = {}  # token_id → Session
        self._revoked_token_ids: set[UUID] = set()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Pairing flow
    # ------------------------------------------------------------------

    def request_pairing(self, *, agent_id: str) -> PairingRequest:
        """AI calls this to request a pairing. Returns the PairingRequest
        with status='pending'. The web layer turns the pairing_id into a
        URL the AI can share with the human."""
        if not agent_id or len(agent_id) > 256:
            raise AdroidError(
                "agent_id must be 1-256 chars",
                code="adroid.session.bad_agent_id",
            )
        with self._lock:
            # Count active sessions — block new pairings if at capacity
            active = sum(1 for s in self._sessions.values() if s.is_active)
            if active >= self._max_sessions:
                raise RateLimitedError(
                    f"max concurrent sessions reached ({self._max_sessions})",
                    code="adroid.session.max_sessions",
                )
            p = PairingRequest(agent_id=agent_id)
            self._pairings[p.pairing_id] = p
            return p

    def get_pairing(self, pairing_id: str) -> PairingRequest | None:
        with self._lock:
            return self._pairings.get(pairing_id)

    def list_pending_pairings(self) -> list[PairingRequest]:
        with self._lock:
            # Expire stale pairings while we're at it
            now = datetime.now(timezone.utc)
            for p in list(self._pairings.values()):
                if (
                    p.status == "pending"
                    and (now - p.requested_at).total_seconds() > self._max_pairing_wait
                ):
                    p.status = "expired"
                    p.decided_at = now
            return [p for p in self._pairings.values() if p.status == "pending"]

    def approve_pairing(
        self,
        *,
        pairing_id: str,
        ttl_seconds: int,
        approver: str,
    ) -> PairingRequest:
        """Human approves a pending pairing with a chosen TTL.

        ``ttl_seconds`` is the user-set expiry. The user can only set this
        once (at approval time). After approval, TTL is fixed.
        """
        if ttl_seconds < 60 or ttl_seconds > 365 * 24 * 3600:
            raise AdroidError(
                f"ttl_seconds must be between 60 and 31536000 (1 year), got {ttl_seconds}",
                code="adroid.session.bad_ttl",
            )
        with self._lock:
            p = self._pairings.get(pairing_id)
            if p is None:
                raise AdroidError(
                    f"unknown pairing_id: {pairing_id}",
                    code="adroid.session.unknown_pairing",
                )
            if p.status != "pending":
                raise AdroidError(
                    f"pairing already decided: {p.status}",
                    code="adroid.session.pairing_already_decided",
                )

            # Mint a session token: ALL capabilities at ACT scope
            all_grants: list[tuple[Capability, Scope]] = [
                (cap, Scope.ACT) for cap in Capability
            ]
            token = self._issuer.issue(
                subject=f"session:{p.agent_id}",
                grants=all_grants,
                ttl=timedelta(seconds=ttl_seconds),
            )

            p.status = "approved"
            p.decided_at = datetime.now(timezone.utc)
            p.decided_by = approver
            p.ttl_seconds = ttl_seconds
            p.session_token_json = token.model_dump_json()
            p.session_id = str(token.token_id)

            session = Session(
                session_id=str(token.token_id),
                token_id=token.token_id,
                agent_id=p.agent_id,
                created_at=datetime.now(timezone.utc),
                expires_at=token.expires_at,
                pairing_id=pairing_id,
            )
            self._sessions[session.session_id] = session
            self._token_to_session[token.token_id] = session
            return p

    def deny_pairing(self, *, pairing_id: str, approver: str) -> PairingRequest:
        with self._lock:
            p = self._pairings.get(pairing_id)
            if p is None:
                raise AdroidError(
                    f"unknown pairing_id: {pairing_id}",
                    code="adroid.session.unknown_pairing",
                )
            if p.status != "pending":
                raise AdroidError(
                    f"pairing already decided: {p.status}",
                    code="adroid.session.pairing_already_decided",
                )
            p.status = "denied"
            p.decided_at = datetime.now(timezone.utc)
            p.decided_by = approver
            return p

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def list_sessions(self, *, include_inactive: bool = False) -> list[Session]:
        with self._lock:
            sessions = list(self._sessions.values())
            if not include_inactive:
                sessions = [s for s in sessions if s.is_active]
            return sessions

    def get_session_by_token(self, token_id: UUID) -> Session | None:
        with self._lock:
            return self._token_to_session.get(token_id)

    def disconnect_session(self, session_id: str) -> bool:
        """Human clicks Disconnect. Returns True if found + revoked."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None or s.revoked:
                return False
            s.revoked = True
            s.revoked_at = datetime.now(timezone.utc)
            self._revoked_token_ids.add(s.token_id)
            return True

    def is_revoked(self, token_id: UUID) -> bool:
        """Gate calls this on every authorize()."""
        with self._lock:
            return token_id in self._revoked_token_ids

    # ------------------------------------------------------------------
    # Periodic cleanup (called by web layer timer or test code)
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Remove expired sessions from the active list. Returns count removed."""
        with self._lock:
            now = datetime.now(timezone.utc)
            removed = 0
            for s in list(self._sessions.values()):
                if not s.revoked and s.expires_at <= now:
                    s.revoked = True
                    s.revoked_at = now
                    self._revoked_token_ids.add(s.token_id)
                    removed += 1
            return removed
