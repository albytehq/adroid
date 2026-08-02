"""Audit schema — canonical serialization for the append-only log.

The audit log is the most stability-sensitive artifact in Adroid. Once an
event is written, it must remain verifiable forever. This module defines:

    1. The canonical JSON shape of an audited event row.
    2. The hash function that chains events together (SHA-256, no surprises).
    3. The signature domain separator (prevents cross-protocol signature
       confusion attacks).

DO NOT change any constant or function name in this module without a
minor-version bump. Existing audit logs must remain readable + verifiable.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from adroid.contract.types import AuditEvent


# Domain separator mixed into the hash chain. If we ever need to migrate
# to a stronger hash function, we bump this constant AND the major version,
# and ship a migration tool that re-hashes old events under the new domain.
AUDIT_HASH_DOMAIN = "adroid.audit.v1"
AUDIT_SIGNATURE_DOMAIN = "adroid.audit.sig.v1"


def canonical_event_payload(event: AuditEvent) -> bytes:
    """Return the exact bytes that ``event_hash`` covers.

    Excludes ``event_hash`` itself (obviously) but INCLUDES ``prev_hash``
    so the chain is tamper-evident. ``sequence`` and ``event_id`` are also
    included to prevent reordering.
    """
    payload: dict[str, Any] = event.model_dump(
        mode="json",
        exclude={"event_hash"},
        exclude_none=False,
    )
    # Force key sort for byte-stable serialization across Python versions.
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def compute_event_hash(event: AuditEvent) -> str:
    """SHA-256 over (DOMAIN || canonical payload). Returns hex."""
    h = hashlib.sha256()
    h.update(AUDIT_HASH_DOMAIN.encode("utf-8"))
    h.update(b"\x00")  # null separator
    h.update(canonical_event_payload(event))
    return h.hexdigest()


def compute_signature_message(
    *,
    prev_hash: str | None,
    event_hash: str,
    payload: bytes,
) -> bytes:
    """The exact bytes the Ed25519 signer signs for one audit event.

    The signature covers:
        - domain separator (prevents cross-protocol confusion)
        - prev_hash (chain position)
        - event_hash (event identity)
        - canonical payload (full event content)

    Including both event_hash AND payload is belt-and-suspenders: if a
    hash collision ever happens, the signature still binds to the content.
    """
    parts = [
        AUDIT_SIGNATURE_DOMAIN.encode("utf-8"),
        b"\x00",
        (prev_hash or "").encode("utf-8"),
        b"\x00",
        event_hash.encode("utf-8"),
        b"\x00",
        payload,
    ]
    return b"".join(parts)
