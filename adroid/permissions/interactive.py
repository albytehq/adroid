"""Interactive permission gate — extends PermissionGate with web-based
approval flow for ACT and ADMIN scope operations.

When the gate is in interactive mode, any operation that requires ACT or
ADMIN scope does NOT immediately succeed after token + scope + rate-limit
checks. Instead, the gate:

    1. Creates a ``PendingApproval`` record with a unique ID.
    2. Publishes it to an async queue that the web UI subscribes to.
    3. Blocks the calling thread until either:
        a. The web UI sends an approval → call proceeds.
        b. The web UI sends a denial → raises ``PermissionDeniedError``.
        c. The timeout (default 60s) elapses → raises ``PermissionDeniedError``.

READ scope operations skip the interactive flow — they're safe enough to
auto-approve after token + rate-limit checks.

This gate is the bridge between AI agent autonomy and human oversight.
Every approval/denial is recorded in the audit log with the approver's
identity (browser session ID for v0.1.0; v0.2.0 will add SSO identities).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from adroid.contract.errors import PermissionDeniedError
from adroid.contract.types import (
    AuditEvent,
    AuditOutcome,
    Capability,
    CapabilityToken,
    Scope,
)
from adroid.permissions.gate import PermissionGate


@dataclass
class PendingApproval:
    """A request waiting for human approval.

    The web UI renders this object; the user taps Approve or Deny; the
    gate unblocks.
    """

    token_id: uuid.UUID
    subject: str
    capability: Capability
    required_scope: Scope
    method: str
    args: dict  # JSON-serializable
    device_id: str | None = None
    approval_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decision: str | None = None  # "approved" | "denied" | "timeout"
    approver: str | None = None  # browser session ID

    def to_dict(self) -> dict:
        return {
            "approval_id": self.approval_id,
            "token_id": str(self.token_id),
            "subject": self.subject,
            "capability": self.capability.value,
            "required_scope": self.required_scope.value,
            "method": self.method,
            "args": self.args,
            "device_id": self.device_id,
            "requested_at": self.requested_at.isoformat(),
            "decided_at": self.decided_at.isoformat() if self.decided_at else None,
            "decision": self.decision,
            "approver": self.approver,
        }


class ApprovalBroker:
    """Thread-safe broker between the gate (producer) and web UI (consumer).

    The gate calls ``request()`` which blocks until the web UI calls
    ``resolve()`` with the matching approval_id. The web UI polls
    ``pending()`` to render the queue, or subscribes via ``on_pending_change``
    for push notifications.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingApproval] = {}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._history: list[PendingApproval] = []  # capped at 100
        self._history_cap = 100
        # Async result store: approval_id → tool result (dict) or Exception
        # Filled by the worker thread that runs the tool after approval.
        self._results: dict[str, dict] = {}
        self._errors: dict[str, Exception] = {}

    def submit(
        self,
        approval: PendingApproval,
    ) -> str:
        """Register an approval WITHOUT blocking. Returns the approval_id.

        Use this for async flow:
            1. Agent POSTs /agent/call → broker.submit(approval) → return 202 + approval_id
            2. Agent polls /agent/result/{approval_id} until decision != None
            3. Browser UI calls /api/approve/{id} or /api/deny/{id}
            4. Worker thread (separate) executes the tool, stores result
            5. /agent/result/{id} returns the final result

        The caller is responsible for executing the tool AFTER approval —
        broker only tracks the decision state.
        """
        with self._lock:
            self._pending[approval.approval_id] = approval
            self._events[approval.approval_id] = threading.Event()
        self._notify_listeners()
        return approval.approval_id

    def wait_for_decision(
        self,
        approval_id: str,
        *,
        timeout_seconds: float = 60.0,
    ) -> PendingApproval | None:
        """Block until a decision is made. Returns the approval (with decision
        filled) or None if not found. Used by worker threads that need to
        block until the user decides."""
        with self._lock:
            approval = self._pending.get(approval_id)
            event = self._events.get(approval_id)
            if approval is None or event is None:
                return None
        decided = event.wait(timeout=timeout_seconds)

        with self._lock:
            approval = self._pending.pop(approval_id, None)
            self._events.pop(approval_id, None)
            if approval is None:
                return None
            if not decided:
                approval.decision = "timeout"
                approval.decided_at = datetime.now(timezone.utc)
            self._history.append(approval)
            if len(self._history) > self._history_cap:
                self._history = self._history[-self._history_cap:]

        self._notify_listeners()
        return approval

    def store_result(self, approval_id: str, result: dict) -> None:
        """Store the tool's result for an approved call. The agent polls
        /agent/result/{approval_id} to retrieve this."""
        with self._lock:
            self._results[approval_id] = result

    def store_error(self, approval_id: str, exc: Exception) -> None:
        """Store an error raised during tool execution."""
        with self._lock:
            self._errors[approval_id] = exc

    def get_result(self, approval_id: str) -> dict | None:
        """Return the stored result, or None if not yet available. Raises
        the stored exception if one was stored instead."""
        with self._lock:
            if approval_id in self._errors:
                raise self._errors[approval_id]
            return self._results.get(approval_id)

    def get_status(self, approval_id: str) -> dict | None:
        """Return the current status of an approval:
            - {state: "pending"} if waiting for decision
            - {state: "approved", decided_at: ..., approver: ...} if approved but result not ready
            - {state: "denied", decided_at: ..., approver: ...} if denied
            - {state: "timeout", decided_at: ...} if timed out
            - {state: "completed", result: {...}} if approved + result stored
            - {state: "error", error: {...}} if approved + error stored
            - None if approval_id is unknown
        """
        with self._lock:
            # Check history first (decided approvals)
            for a in reversed(self._history):
                if a.approval_id == approval_id:
                    if approval_id in self._errors:
                        exc = self._errors[approval_id]
                        return {
                            "state": "error",
                            "decision": a.decision,
                            "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                            "approver": a.approver,
                            "error": {
                                "type": type(exc).__name__,
                                "message": str(exc),
                            },
                        }
                    if approval_id in self._results:
                        return {
                            "state": "completed",
                            "decision": a.decision,
                            "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                            "approver": a.approver,
                            "result": self._results[approval_id],
                        }
                    return {
                        "state": a.decision or "pending",
                        "decided_at": a.decided_at.isoformat() if a.decided_at else None,
                        "approver": a.approver,
                    }
            # Check pending
            if approval_id in self._pending:
                return {"state": "pending"}
            return None

    def request(
        self,
        approval: PendingApproval,
        *,
        timeout_seconds: float = 60.0,
    ) -> PendingApproval:
        """Block until a decision is made or timeout. Returns the decided
        approval (with decision + approver filled)."""
        event = threading.Event()
        with self._lock:
            self._pending[approval.approval_id] = approval
            self._events[approval.approval_id] = event
        self._notify_listeners()

        try:
            decided = event.wait(timeout=timeout_seconds)
        finally:
            with self._lock:
                self._pending.pop(approval.approval_id, None)
                self._events.pop(approval.approval_id, None)

        if not decided:
            approval.decision = "timeout"
            approval.decided_at = datetime.now(timezone.utc)
        # If decided, the resolve() call already filled decision + decided_at + approver

        with self._lock:
            self._history.append(approval)
            if len(self._history) > self._history_cap:
                self._history = self._history[-self._history_cap:]

        self._notify_listeners()
        return approval

    def resolve(
        self,
        approval_id: str,
        *,
        decision: str,
        approver: str | None = None,
    ) -> bool:
        """Web UI calls this. Returns True if the approval was found and
        resolved, False if it was already gone (timeout or already decided)."""
        with self._lock:
            approval = self._pending.get(approval_id)
            event = self._events.get(approval_id)
            if approval is None or event is None:
                return False
            approval.decision = decision
            approval.decided_at = datetime.now(timezone.utc)
            approval.approver = approver
            event.set()
        self._notify_listeners()
        return True

    def pending(self) -> list[PendingApproval]:
        with self._lock:
            return list(self._pending.values())

    def history(self, limit: int = 50) -> list[PendingApproval]:
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def on_pending_change(self, callback: Callable[[], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                pass  # listener errors must not break the broker


class InteractiveGate(PermissionGate):
    """Permission gate that requires interactive approval for ACT/ADMIN scope.

    Construct with an ``ApprovalBroker``. The web UI shares the same broker
    instance to receive pending requests and send decisions.
    """

    def __init__(
        self,
        *,
        issuer_id: str,
        public_key,  # Ed25519PublicKey
        broker: ApprovalBroker,
        approval_timeout_seconds: float = 60.0,
        require_approval_for_read: bool = False,
        rate_limiter=None,
    ):
        super().__init__(
            issuer_id=issuer_id,
            public_key=public_key,
            rate_limiter=rate_limiter,
        )
        self._broker = broker
        self._approval_timeout = approval_timeout_seconds
        self._require_for_read = require_approval_for_read

    def authorize_interactive(
        self,
        token: CapabilityToken,
        capability: Capability,
        required_scope: Scope,
        *,
        method: str,
        args: dict,
        device_id: str | None = None,
    ) -> PendingApproval | None:
        """Authorize with interactive approval for ACT/ADMIN scope.

        Returns:
            - ``None`` if no approval was needed (READ scope, auto-approved).
            - ``PendingApproval`` (with decision="approved") if human approved.

        Raises:
            - ``PermissionDeniedError`` if token invalid, scope insufficient,
              rate limited, human denied, or approval timed out.
        """
        # Step 1: standard token + scope + rate-limit checks (may raise)
        self.authorize(token, capability, required_scope)

        # Step 2: decide if interactive approval is needed
        needs_approval = (
            self._require_for_read
            or required_scope in (Scope.ACT, Scope.ADMIN)
        )
        if not needs_approval:
            return None

        # Step 3: request interactive approval (blocks)
        approval = PendingApproval(
            token_id=token.token_id,
            subject=token.subject,
            capability=capability,
            required_scope=required_scope,
            method=method,
            args=args,
            device_id=device_id,
        )
        decided = self._broker.request(
            approval, timeout_seconds=self._approval_timeout
        )

        if decided.decision != "approved":
            raise PermissionDeniedError(
                f"interactive approval {decided.decision}: {capability.value} at {required_scope.value}",
                code=f"adroid.permission.interactive_{decided.decision}",
                details={
                    "approval_id": decided.approval_id,
                    "capability": capability.value,
                    "scope": required_scope.value,
                    "approver": decided.approver,
                    "decided_at": decided.decided_at.isoformat() if decided.decided_at else None,
                },
            )

        return decided
