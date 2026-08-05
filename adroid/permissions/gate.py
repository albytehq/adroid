"""Permission gate — the single chokepoint every tool call must pass through.

The gate is the only place in the runtime that says "yes" or "no" to a tool
call. It does four things, in order:

    0. Check token revocation (cheapest, never mutates state).
    1. Validate the token (signature, expiry, issuer).
    2. Check the token grants the requested capability at the required scope.
    3. Enforce per-token rate limits (in-memory token bucket per capability).

If any check fails, the gate raises ``PermissionDeniedError`` (or
``RateLimitedError``) and the runtime logs the denial to the audit trail
with ``outcome=DENIED``. The gate NEVER returns False silently.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from uuid import UUID

from adroid.contract.errors import PermissionDeniedError, RateLimitedError
from adroid.contract.types import Capability, CapabilityToken, Scope
from adroid.permissions.tokens import validate_token_at
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


# ---------------------------------------------------------------------------
# In-memory rate limiter — simple token bucket per (token_id, capability)
# ---------------------------------------------------------------------------


@dataclass
class _Bucket:
    capacity: float
    tokens: float
    refill_rate: float  # tokens per second
    last_refill: float = field(default_factory=time.monotonic)

    def consume(self, now: float, amount: float = 1.0) -> bool:
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


class RateLimiter:
    """Thread-safe in-memory rate limiter.

    Uses ``time.monotonic()`` by default. For testing with frozen/advancing
    time, inject a custom ``clock`` callable returning monotonic seconds.
    The ``now: datetime`` parameter on ``check_and_consume`` is accepted
    for API compatibility with ``PermissionGate.authorize`` but is NOT
    used — monotonic time is the correct choice for rate-limit math because
    it is immune to system clock adjustments.

    Uses in-memory state (single-process). The public API
    (``check_and_consume``) will not change.
    """

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._buckets: dict[tuple[str, Capability], _Bucket] = {}
        self._lock = threading.Lock()
        self._clock = clock or time.monotonic

    def check_and_consume(
        self,
        token: CapabilityToken,
        capability: Capability,
        *,
        now: datetime | None = None,  # accepted for API compat; ignored
    ) -> None:
        """Consume one call from the bucket. Raises RateLimitedError if empty."""
        grant = next(
            (g for g in token.grants if g.capability == capability),
            None,
        )
        # No per-grant rate limit means: fall back to runtime default.
        # v0.1.0 default = 60/min = 1/sec, capacity 60 (burst a full minute).
        per_minute = grant.rate_limit_per_minute if grant and grant.rate_limit_per_minute is not None else 60
        # Token bucket: capacity = burst size, refill_rate = sustained rate.
        # We allow bursting up to one minute's worth of quota at once, capped
        # at 100 to prevent silly values from exhausting memory.
        capacity = max(1.0, min(float(per_minute), 100.0))
        refill = per_minute / 60.0

        key = (str(token.token_id), capability)
        with self._lock:
            now_mono = self._clock()
            bucket = self._buckets.get(key)
            if bucket is None:
                # Pass last_refill explicitly — the default_factory would
                # capture a slightly later time than now_mono, producing a
                # negative elapsed on the first consume() and falsely
                # rate-limiting the very first call.
                bucket = _Bucket(
                    capacity=capacity,
                    tokens=capacity,
                    refill_rate=refill,
                    last_refill=now_mono,
                )
                self._buckets[key] = bucket
            # Update capacity in case the grant changed (rare but possible).
            # Clamp tokens to the new capacity so a tighter grant doesn't
            # leave the bucket holding more than its declared burst size.
            bucket.capacity = capacity
            bucket.refill_rate = refill
            bucket.tokens = min(bucket.tokens, capacity)
            if not bucket.consume(now_mono):
                raise RateLimitedError(
                    f"rate limit exceeded for {capability.value}",
                    code="adroid.rate_limited",
                    details={
                        "token_id": str(token.token_id),
                        "capability": capability.value,
                        "per_minute": per_minute,
                    },
                )


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


class PermissionGate:
    """Validates tokens and enforces scope + rate-limit policy.

    Construct one per runtime. The ``public_key`` is the issuer's
    Ed25519 public key — must match the private key the ``TokenIssuer``
    uses to sign tokens.
    """

    def __init__(
        self,
        *,
        issuer_id: str,
        public_key: Ed25519PublicKey,
        rate_limiter: RateLimiter | None = None,
        revocation_checker: Callable[[UUID], bool] | None = None,
    ):
        self._issuer_id = issuer_id
        self._public_key = public_key
        self._limiter = rate_limiter or RateLimiter()
        # Defense-in-depth: the gate consults this on every authorize().
        # Wire it to SessionManager.is_revoked at construction time.
        self._is_revoked: Callable[[UUID], bool] = revocation_checker or (lambda _tid: False)

    def authorize(
        self,
        token: CapabilityToken,
        capability: Capability,
        required_scope: Scope,
        *,
        now: datetime | None = None,
    ) -> None:
        """Authorize a call. Raises on denial. Returns None on success.

        Order matters:
            0. Revocation (cheapest, never mutates state, never depends on time)
            1. Signature + expiry + issuer (cheapest, deterministic)
            2. Capability + scope presence
            3. Rate limit (mutates state — only after we know the call is allowed)

        If any check fails, the runtime layer is responsible for writing the
        audit event. The gate itself never touches the audit log — separation
        of concerns.
        """
        # 0. Revocation — defense-in-depth. The web layer also checks this,
        #    but the gate is the documented single chokepoint.
        if self._is_revoked(token.token_id):
            raise PermissionDeniedError(
                "token has been revoked",
                code="adroid.token.revoked",
                details={"token_id": str(token.token_id)},
            )

        # 1. Token validity
        validate_token_at(
            token,
            self._public_key,
            now=now,
            expected_issuer=self._issuer_id,
        )

        # 2. Capability + scope
        if not token.has(capability, required_scope):
            raise PermissionDeniedError(
                f"token does not grant {capability.value} at scope {required_scope.value}",
                code="adroid.permission.insufficient_scope",
                details={
                    "token_id": str(token.token_id),
                    "required_capability": capability.value,
                    "required_scope": required_scope.value,
                    "granted": [
                        f"{g.capability.value}:{g.scope.value}" for g in token.grants
                    ],
                },
            )

        # 3. Rate limit
        self._limiter.check_and_consume(token, capability, now=now)
