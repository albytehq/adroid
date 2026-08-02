"""Capability token issuance & verification.

Tokens are Ed25519-signed JSON documents. The signature is detached and
covers the canonical JSON of every field except ``signature`` itself.

Canonicalization rules (STABLE — do not change without a minor bump):
    1. Serialize with ``model_dump(mode="json", exclude={"signature"})``.
    2. Sort object keys recursively and alphabetically.
    3. No whitespace, no trailing newline.
    4. ``ensure_ascii=False`` so non-ASCII chars in subject survive byte-stable.

These rules matter because any verifier (in any language) must produce the
exact same byte string. If we change them, every previously issued token
becomes unverifiable.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from adroid.contract.errors import ContractError, PermissionDeniedError
from adroid.contract.types import Capability, CapabilityGrant, CapabilityToken, Scope


def generate_signing_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate a fresh Ed25519 keypair. The runtime keeps the private key
    in process memory; only the public key is persisted for verifiers.
    """
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def serialize_public_key(public_key: Ed25519PublicKey) -> str:
    """PEM-encoded SubjectPublicKeyInfo. Safe to ship in a config file."""
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")


def serialize_private_key(private_key: Ed25519PrivateKey) -> str:
    """PKCS8 PEM. MUST be stored in a secret manager, never on disk in plain."""
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


def load_public_key(pem: str) -> Ed25519PublicKey:
    return serialization.load_pem_public_key(pem.encode("ascii"))  # type: ignore[return-value]


def load_private_key(pem: str) -> Ed25519PrivateKey:
    return serialization.load_pem_private_key(  # type: ignore[return-value]
        pem.encode("ascii"), password=None
    )


# ---------------------------------------------------------------------------
# Canonical serialization — see module docstring for STABILITY contract
# ---------------------------------------------------------------------------


def canonical_token_payload(token: CapabilityToken) -> bytes:
    """Return the exact bytes the signature covers.

    Do NOT call this from outside the permissions module. The shape is a
    stable contract, but the function itself is not.

    IMPORTANT: frozenset[CapabilityGrant] serializes as a JSON array, but
    sets have non-deterministic iteration order in Python. We MUST sort
    the grants array by a stable key (capability.value) before serializing,
    otherwise the same token will produce different canonical bytes on
    different runs — and signatures will not verify.
    """
    payload = token.model_dump(mode="json", exclude={"signature"}, exclude_none=False)
    # Sort grants deterministically by (capability, scope) so the canonical
    # form is byte-stable regardless of frozenset iteration order.
    if "grants" in payload and isinstance(payload["grants"], list):
        payload["grants"] = sorted(
            payload["grants"],
            key=lambda g: (g.get("capability", ""), g.get("scope", "")),
        )
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_token(token: CapabilityToken, private_key: Ed25519PrivateKey) -> CapabilityToken:
    """Attach a detached Ed25519 signature to a token.

    Returns a NEW frozen token instance with the ``signature`` field filled.
    """
    if token.signature and token.signature != "0" * 128:
        raise ContractError("token is already signed", code="adroid.token.already_signed")
    signature = private_key.sign(canonical_token_payload(token))
    return token.model_copy(update={"signature": signature.hex()})


def verify_token(token: CapabilityToken, public_key: Ed25519PublicKey) -> None:
    """Verify the signature on a token. Raises PermissionDeniedError on failure."""
    try:
        public_key.verify(
            bytes.fromhex(token.signature),
            canonical_token_payload(token),
        )
    except (InvalidSignature, ValueError) as exc:
        raise PermissionDeniedError(
            "token signature verification failed",
            code="adroid.token.bad_signature",
            details={"token_id": str(token.token_id)},
        ) from exc


# ---------------------------------------------------------------------------
# Issuance convenience
# ---------------------------------------------------------------------------


class TokenIssuer:
    """Stateful issuer bound to a runtime's signing key.

    The runtime constructs one of these at boot and uses it to mint tokens
    for integrators. Tokens are returned already signed.
    """

    def __init__(
        self,
        *,
        issuer_id: str,
        signing_key: Ed25519PrivateKey,
        public_key: Ed25519PublicKey,
    ):
        self._issuer_id = issuer_id
        self._private = signing_key
        self._public = public_key

    @property
    def issuer_id(self) -> str:
        return self._issuer_id

    @property
    def public_key_pem(self) -> str:
        return serialize_public_key(self._public)

    def issue(
        self,
        *,
        subject: str,
        grants: Iterable[CapabilityGrant | tuple[Capability, Scope]],
        ttl: timedelta = timedelta(hours=1),
    ) -> CapabilityToken:
        """Mint a signed token for ``subject`` carrying ``grants``.

        ``grants`` may be either ``CapabilityGrant`` objects or ``(capability,
        scope)`` tuples for convenience. Rate limits default to runtime quota.
        """
        normalized: list[CapabilityGrant] = []
        for g in grants:
            if isinstance(g, CapabilityGrant):
                normalized.append(g)
            else:
                cap, scope = g
                normalized.append(CapabilityGrant(capability=cap, scope=scope))

        now = datetime.now(timezone.utc)
        unsigned = CapabilityToken(
            issuer=self._issuer_id,
            subject=subject,
            issued_at=now,
            expires_at=now + ttl,
            grants=frozenset(normalized),
            # placeholder — overwritten by sign_token
            signature="0" * 128,
        )
        try:
            return sign_token(unsigned, self._private)
        except Exception as exc:
            raise ContractError(
                f"failed to sign token: {exc}",
                code="adroid.token.sign_failed",
            ) from exc


def validate_token_at(
    token: CapabilityToken,
    public_key: Ed25519PublicKey,
    *,
    now: datetime | None = None,
    expected_issuer: str | None = None,
) -> None:
    """Full validation: signature, expiry, optional issuer match.

    Use this at the gate, not bare ``verify_token``.
    """
    verify_token(token, public_key)

    now = now or datetime.now(timezone.utc)
    if now >= token.expires_at:
        raise PermissionDeniedError(
            "token has expired",
            code="adroid.token.expired",
            details={
                "token_id": str(token.token_id),
                "expired_at": token.expires_at.isoformat(),
            },
        )
    if expected_issuer and token.issuer != expected_issuer:
        raise PermissionDeniedError(
            "token issuer mismatch",
            code="adroid.token.issuer_mismatch",
            details={
                "expected": expected_issuer,
                "actual": token.issuer,
            },
        )

    # Pydantic re-validation catches any field-shape tampering that a
    # signer might have tried between construction and now. We use the JSON
    # round-trip because frozenset[Model] does not dump cleanly to pure
    # Python dicts (sets of dicts are unhashable).
    try:
        CapabilityToken.model_validate_json(token.model_dump_json())
    except ValidationError as exc:
        raise PermissionDeniedError(
            "token failed re-validation after signature check",
            code="adroid.token.malformed",
            details={"errors": exc.errors()},
        ) from exc
