"""Audit layer — append-only, hash-chained, Ed25519-signed event log."""

from adroid.audit.schema import (
    AUDIT_HASH_DOMAIN,
    AUDIT_SIGNATURE_DOMAIN,
    canonical_event_payload,
    compute_event_hash,
    compute_signature_message,
)
from adroid.audit.writer import AuditReader, AuditWriter

__all__ = [
    "AUDIT_HASH_DOMAIN",
    "AUDIT_SIGNATURE_DOMAIN",
    "AuditReader",
    "AuditWriter",
    "canonical_event_payload",
    "compute_event_hash",
    "compute_signature_message",
]
