"""Tests for the audit layer — append-only, hash-chained, signed."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from adroid.audit.schema import (
    AUDIT_HASH_DOMAIN,
    canonical_event_payload,
    compute_event_hash,
    compute_signature_message,
)
from adroid.audit.writer import AuditReader, AuditWriter
from adroid.contract.errors import AuditError
from adroid.contract.types import AuditEvent, AuditOutcome, Capability, Scope
from adroid.permissions.tokens import generate_signing_keypair


@pytest.fixture
def audit_setup(tmp_path):
    private, public = generate_signing_keypair()
    log_path = tmp_path / "test.auditlog"
    writer = AuditWriter(
        log_path=log_path,
        signing_key=private,
        public_key=public,
    )
    reader = AuditReader(log_path=log_path, public_key=public)
    return writer, reader, log_path


def _make_event(**overrides) -> AuditEvent:
    base = dict(
        outcome=AuditOutcome.SUCCESS,
        method="runtime.device_list",
        args_digest="0" * 64,
        capability=Capability.DEVICE_LIST,
        scope=Scope.READ,
    )
    base.update(overrides)
    return AuditEvent(**base)


# ---------------------------------------------------------------------------
# Schema / canonicalization
# ---------------------------------------------------------------------------


def test_canonical_payload_excludes_event_hash():
    e = _make_event()
    payload = canonical_event_payload(e)
    obj = json.loads(payload)
    assert "event_hash" not in obj


def test_compute_event_hash_is_deterministic():
    """Two events with identical fields must hash identically."""
    fixed_uuid = uuid4()
    fixed_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    common = dict(event_id=fixed_uuid, timestamp=fixed_ts)
    e1 = _make_event(**common)
    e2 = _make_event(**common)
    assert compute_event_hash(e1) == compute_event_hash(e2)


def test_compute_event_hash_changes_on_field_change():
    fixed_uuid = uuid4()
    fixed_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    common = dict(event_id=fixed_uuid, timestamp=fixed_ts)
    e1 = _make_event(method="runtime.device_list", **common)
    e2 = _make_event(method="runtime.device_observe", **common)
    assert compute_event_hash(e1) != compute_event_hash(e2)


def test_compute_event_hash_includes_prev_hash():
    """The chain position must affect the hash — otherwise reordering is
    undetectable."""
    fixed_uuid = uuid4()
    fixed_ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    common = dict(event_id=fixed_uuid, timestamp=fixed_ts)
    e1 = _make_event(**common)
    e2 = _make_event(prev_hash="0" * 64, **common)
    assert compute_event_hash(e1) != compute_event_hash(e2)


def test_signature_message_includes_domain_separator():
    msg = compute_signature_message(
        prev_hash=None,
        event_hash="0" * 64,
        payload=b"{}",
    )
    assert AUDIT_HASH_DOMAIN.encode("utf-8") not in msg  # different domain
    from adroid.audit.schema import AUDIT_SIGNATURE_DOMAIN
    assert AUDIT_SIGNATURE_DOMAIN.encode("utf-8") in msg


# ---------------------------------------------------------------------------
# Writer / Reader round-trip
# ---------------------------------------------------------------------------


def test_writer_appends_events_sequentially(audit_setup):
    writer, reader, _ = audit_setup
    e1 = writer.write(_make_event())
    e2 = writer.write(_make_event())

    assert e1.sequence == 0
    assert e2.sequence == 1
    assert e1.prev_hash is None  # first event
    assert e2.prev_hash == e1.event_hash
    assert e1.event_hash is not None
    assert e2.event_hash != e1.event_hash


def test_reader_verifies_chain_intact(audit_setup):
    writer, reader, _ = audit_setup
    for _ in range(5):
        writer.write(_make_event())

    results = list(reader.verify_all())
    assert len(results) == 5
    for seq, ok, err in results:
        assert ok, f"sequence {seq} failed: {err}"


def test_reader_detects_tampered_payload(audit_setup):
    writer, reader, log_path = audit_setup
    writer.write(_make_event())
    writer.write(_make_event())

    # Tamper with the first line's payload
    lines = log_path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["payload"]["method"] = "tampered"
    lines[0] = json.dumps(rec, separators=(",", ":"))
    log_path.write_text("\n".join(lines) + "\n")

    results = list(reader.verify_all())
    # First line should fail (event_hash mismatch or signature failure)
    assert any(not ok for _, ok, _ in results)


def test_reader_detects_truncated_line(audit_setup, tmp_path):
    """A crash mid-write should produce a truncated final line — recovery
    should treat it as missing and resume from the prior good line."""
    writer, reader, log_path = audit_setup
    writer.write(_make_event())
    writer.write(_make_event())

    # Append a truncated line
    with open(log_path, "ab") as f:
        f.write(b'{"sequence": 2, "incomplete"')

    # New writer should recover and continue from sequence 2
    private, public = generate_signing_keypair()
    writer2 = AuditWriter(
        log_path=log_path,
        signing_key=private,  # different key just for recovery test
        public_key=public,
    )
    # Recovered sequence should be 2 (after the 2 valid events)
    assert writer2._sequence == 2  # noqa: SLF001
    assert writer2._prev_hash is not None  # noqa: SLF001


# ---------------------------------------------------------------------------
# Metadata size cap
# ---------------------------------------------------------------------------


def test_writer_rejects_oversized_metadata(audit_setup):
    writer, _, _ = audit_setup
    big_metadata = {"k": "v" * 8192}
    e = _make_event(metadata=big_metadata)
    with pytest.raises(AuditError) as exc:
        writer.write(e)
    assert exc.value.code == "adroid.audit.metadata_too_large"


def test_writer_accepts_normal_metadata(audit_setup):
    writer, reader, _ = audit_setup
    e = _make_event(metadata={"agent": "foo", "duration_ms": 42})
    written = writer.write(e)
    assert written.metadata == {"agent": "foo", "duration_ms": 42}


# ---------------------------------------------------------------------------
# Recovery across instances
# ---------------------------------------------------------------------------


def test_writer_recovers_sequence_across_instances(audit_setup):
    writer1, _, log_path = audit_setup
    for i in range(3):
        writer1.write(_make_event())

    private, public = generate_signing_keypair()
    writer2 = AuditWriter(
        log_path=log_path,
        signing_key=private,
        public_key=public,
    )
    # The new writer should pick up where the old one left off
    assert writer2._sequence == 3  # noqa: SLF001
    assert writer2._prev_hash is not None  # noqa: SLF001

    # And the next event should chain correctly
    e = writer2.write(_make_event())
    assert e.sequence == 3
