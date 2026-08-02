"""Tests for the permission layer — token issuance, verification, gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from adroid.contract.errors import PermissionDeniedError, RateLimitedError
from adroid.contract.types import Capability, CapabilityGrant, CapabilityToken, Scope
from adroid.permissions.gate import PermissionGate, RateLimiter
from adroid.permissions.tokens import (
    TokenIssuer,
    canonical_token_payload,
    generate_signing_keypair,
    load_private_key,
    load_public_key,
    serialize_private_key,
    serialize_public_key,
    sign_token,
    validate_token_at,
    verify_token,
)


# ---------------------------------------------------------------------------
# Key pair serialization round-trip
# ---------------------------------------------------------------------------


def test_keypair_serialization_roundtrip():
    private, public = generate_signing_keypair()
    priv_pem = serialize_private_key(private)
    pub_pem = serialize_public_key(public)
    assert "BEGIN PRIVATE KEY" in priv_pem
    assert "BEGIN PUBLIC KEY" in pub_pem

    priv2 = load_private_key(priv_pem)
    pub2 = load_public_key(pub_pem)
    # Round-trip should produce equivalent keys (we verify by signing + verifying)
    issuer = TokenIssuer(issuer_id="rt", signing_key=priv2, public_key=pub2)
    token = issuer.issue(subject="x", grants=[(Capability.DEVICE_LIST, Scope.READ)])
    verify_token(token, public)  # original public key should verify


# ---------------------------------------------------------------------------
# Token issuance + signature
# ---------------------------------------------------------------------------


def test_issue_token_produces_valid_signature():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    token = issuer.issue(
        subject="agent:foo",
        grants=[(Capability.DEVICE_LIST, Scope.READ), (Capability.APP_LAUNCH, Scope.ACT)],
        ttl=timedelta(minutes=30),
    )
    # Should not raise
    verify_token(token, public)
    validate_token_at(token, public, expected_issuer="adroid-test")


def test_token_signature_tamper_detection():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    token = issuer.issue(subject="x", grants=[(Capability.DEVICE_LIST, Scope.READ)])

    # Flip one byte in the signature
    bad_sig = bytearray.fromhex(token.signature)
    bad_sig[0] ^= 0x01
    tampered = token.model_copy(update={"signature": bad_sig.hex()})

    with pytest.raises(PermissionDeniedError) as exc:
        verify_token(tampered, public)
    assert exc.value.code == "adroid.token.bad_signature"


def test_token_payload_tamper_detection():
    """If someone mints a token with a different subject but keeps the
    signature, verification must fail."""
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    token = issuer.issue(subject="agent:foo", grants=[(Capability.DEVICE_LIST, Scope.READ)])

    tampered = token.model_copy(update={"subject": "agent:evil"})
    with pytest.raises(PermissionDeniedError):
        verify_token(tampered, public)


def test_token_expiry_validation():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    token = issuer.issue(
        subject="x",
        grants=[(Capability.DEVICE_LIST, Scope.READ)],
        ttl=timedelta(seconds=1),
    )

    # Future timestamp — should still be valid
    validate_token_at(token, public, now=datetime.now(timezone.utc))

    # Past expiry — should fail
    with pytest.raises(PermissionDeniedError) as exc:
        validate_token_at(
            token,
            public,
            now=datetime.now(timezone.utc) + timedelta(seconds=2),
        )
    assert exc.value.code == "adroid.token.expired"


def test_token_issuer_mismatch():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-A", signing_key=private, public_key=public)
    token = issuer.issue(subject="x", grants=[(Capability.DEVICE_LIST, Scope.READ)])

    # Different issuer ID — should fail
    with pytest.raises(PermissionDeniedError) as exc:
        validate_token_at(token, public, expected_issuer="adroid-B")
    assert exc.value.code == "adroid.token.issuer_mismatch"


# ---------------------------------------------------------------------------
# Gate: capability + scope enforcement
# ---------------------------------------------------------------------------


def _make_gate() -> tuple[PermissionGate, TokenIssuer]:
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    gate = PermissionGate(issuer_id="adroid-test", public_key=public)
    return gate, issuer


def test_gate_authorizes_present_capability():
    gate, issuer = _make_gate()
    token = issuer.issue(subject="x", grants=[(Capability.DEVICE_LIST, Scope.READ)])
    gate.authorize(token, Capability.DEVICE_LIST, Scope.READ)


def test_gate_denies_missing_capability():
    gate, issuer = _make_gate()
    token = issuer.issue(subject="x", grants=[(Capability.DEVICE_LIST, Scope.READ)])
    with pytest.raises(PermissionDeniedError) as exc:
        gate.authorize(token, Capability.APP_LAUNCH, Scope.READ)
    assert exc.value.code == "adroid.permission.insufficient_scope"


def test_gate_denies_insufficient_scope():
    gate, issuer = _make_gate()
    token = issuer.issue(subject="x", grants=[(Capability.APP_LAUNCH, Scope.READ)])
    # Tool requires ACT but token only grants READ
    with pytest.raises(PermissionDeniedError):
        gate.authorize(token, Capability.APP_LAUNCH, Scope.ACT)


def test_gate_allows_higher_scope_for_lower_requirement():
    gate, issuer = _make_gate()
    token = issuer.issue(subject="x", grants=[(Capability.DEVICE_LIST, Scope.ADMIN)])
    # Tool requires READ, token has ADMIN — should pass
    gate.authorize(token, Capability.DEVICE_LIST, Scope.READ)


# ---------------------------------------------------------------------------
# Gate: rate limiting
# ---------------------------------------------------------------------------


def test_rate_limiter_consumes_tokens():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    gate = PermissionGate(
        issuer_id="adroid-test",
        public_key=public,
        rate_limiter=RateLimiter(),
    )
    token = issuer.issue(
        subject="x",
        grants=[CapabilityGrant(capability=Capability.DEVICE_LIST, scope=Scope.READ, rate_limit_per_minute=2)],
    )

    # First two should pass
    gate.authorize(token, Capability.DEVICE_LIST, Scope.READ)
    gate.authorize(token, Capability.DEVICE_LIST, Scope.READ)

    # Third should rate-limit
    with pytest.raises(RateLimitedError) as exc:
        gate.authorize(token, Capability.DEVICE_LIST, Scope.READ)
    assert exc.value.code == "adroid.rate_limited"


def test_rate_limiter_isolated_per_capability():
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    gate = PermissionGate(
        issuer_id="adroid-test",
        public_key=public,
        rate_limiter=RateLimiter(),
    )
    token = issuer.issue(
        subject="x",
        grants=[
            CapabilityGrant(capability=Capability.DEVICE_LIST, scope=Scope.READ, rate_limit_per_minute=1),
            CapabilityGrant(capability=Capability.DEVICE_OBSERVE, scope=Scope.READ, rate_limit_per_minute=1),
        ],
    )

    # Exhaust DEVICE_LIST
    gate.authorize(token, Capability.DEVICE_LIST, Scope.READ)
    with pytest.raises(RateLimitedError):
        gate.authorize(token, Capability.DEVICE_LIST, Scope.READ)

    # DEVICE_OBSERVE still has budget
    gate.authorize(token, Capability.DEVICE_OBSERVE, Scope.READ)


# ---------------------------------------------------------------------------
# Canonical payload stability
# ---------------------------------------------------------------------------


def test_canonical_payload_is_byte_stable():
    """The canonical payload bytes must be deterministic across runs.
    Changing this would break every previously issued token."""
    private, public = generate_signing_keypair()
    issuer = TokenIssuer(issuer_id="adroid-test", signing_key=private, public_key=public)
    token = issuer.issue(
        subject="agent:stable",
        grants=[(Capability.DEVICE_LIST, Scope.READ)],
        ttl=timedelta(hours=1),
    )

    payload1 = canonical_token_payload(token)
    payload2 = canonical_token_payload(token)

    assert payload1 == payload2
    # Must be valid JSON with sorted keys
    import json
    obj = json.loads(payload1)
    keys = list(obj.keys())
    assert keys == sorted(keys)
    # signature must be excluded
    assert "signature" not in obj
