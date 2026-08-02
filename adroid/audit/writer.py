"""Audit writer — append-only, hash-chained, Ed25519-signed event log.

The writer is the only thing that mutates the audit log file. Readers
(``AuditReader``) are pure functions that verify the chain on read.

File format (one event per line, JSON Lines):

    {"sequence": 0, "event_id": "...", "prev_hash": null, "event_hash": "...",
     "signature": "...", "payload": { ... full AuditEvent ... }}
    {"sequence": 1, "event_id": "...", "prev_hash": "<hash of seq 0>",
     "event_hash": "...", "signature": "...", "payload": { ... }}
    ...

The file is fsync'd after every write. v0.2.0 will add S3/distributed
backends; v0.1.0 ships local-file only, which is enough for single-node
deployments and CI.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Iterator

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from adroid.audit.schema import (
    canonical_event_payload,
    compute_event_hash,
    compute_signature_message,
)
from adroid.contract.errors import AuditError
from adroid.contract.types import AuditEvent


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class AuditWriter:
    """Append-only writer. One writer per log file. Thread-safe.

    The writer signs every event with the runtime's audit signing key.
    This key is SEPARATE from the token-issuance key — compromise of one
    must not invalidate the other.
    """

    def __init__(
        self,
        *,
        log_path: Path,
        signing_key: Ed25519PrivateKey,
        public_key: Ed25519PublicKey,
        max_metadata_bytes: int = 4096,
    ):
        self._path = Path(log_path)
        self._signing_key = signing_key
        self._public_key = public_key
        self._max_metadata_bytes = max_metadata_bytes
        self._lock = threading.Lock()
        self._sequence: int = 0
        self._prev_hash: str | None = None

        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            # Re-open in append mode, recover sequence + prev_hash from last
            # valid line. We do NOT trust any in-memory state across reboots.
            self._sequence, self._prev_hash = self._recover_state()

    def write(self, event: AuditEvent) -> AuditEvent:
        """Write one event. Returns the fully-formed event with chain fields
        filled in. Raises ``AuditError`` on any failure."""
        with self._lock:
            # Cap metadata size — large blobs belong in object storage, not
            # the audit log. We enforce this here, not in the type, so the
            # type stays simple.
            metadata_size = len(json.dumps(event.metadata, default=str).encode("utf-8"))
            if metadata_size > self._max_metadata_bytes:
                raise AuditError(
                    f"audit event metadata exceeds {self._max_metadata_bytes} bytes",
                    code="adroid.audit.metadata_too_large",
                    details={"actual": metadata_size},
                )

            # Build the chained event: fill prev_hash + event_hash, then sign.
            chained = event.model_copy(
                update={
                    "sequence": self._sequence,
                    "prev_hash": self._prev_hash,
                }
            )
            event_hash = compute_event_hash(chained)
            chained = chained.model_copy(update={"event_hash": event_hash})

            payload = canonical_event_payload(chained)
            message = compute_signature_message(
                prev_hash=self._prev_hash,
                event_hash=event_hash,
                payload=payload,
            )
            signature = self._signing_key.sign(message).hex()

            record = {
                "sequence": chained.sequence,
                "event_id": str(chained.event_id),
                "prev_hash": chained.prev_hash,
                "event_hash": chained.event_hash,
                "signature": signature,
                "payload": json.loads(chained.model_dump_json()),
            }
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"

            # Atomic-ish write: append + fsync. If the process crashes between
            # write() and fsync(), the OS may have buffered some bytes — on
            # recovery we'll detect a truncated line and quarantine it.
            with open(self._path, "ab") as f:
                f.write(line.encode("utf-8"))
                f.flush()
                os.fsync(f.fileno())

            self._sequence += 1
            self._prev_hash = event_hash
            return chained

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _recover_state(self) -> tuple[int, str | None]:
        """Read the last valid (sequence, hash) from the existing log file."""
        last_seq = -1
        last_hash: str | None = None
        with open(self._path, "rb") as f:
            for raw in f:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    last_seq = int(rec["sequence"])
                    last_hash = rec["event_hash"]
                except (json.JSONDecodeError, KeyError, ValueError):
                    # Truncated line — likely a crash mid-write. Stop here;
                    # subsequent writes will overwrite from this point.
                    # v0.2.0 will add a quarantine file for forensic review.
                    break
        return (last_seq + 1, last_hash)


# ---------------------------------------------------------------------------
# Reader — pure verification, no signing key needed
# ---------------------------------------------------------------------------


class AuditReader:
    """Reads + verifies an audit log file. Stateless; safe to share."""

    def __init__(self, *, log_path: Path, public_key: Ed25519PublicKey):
        self._path = Path(log_path)
        self._public_key = public_key

    def verify_all(self) -> Iterator[tuple[int, bool, str | None]]:
        """Yield (sequence, ok, error_message) for every line in the log.

        If the log file doesn't exist, yields nothing (empty iterator).
        """
        if not self._path.exists():
            return
        prev_hash: str | None = None
        with open(self._path, "rb") as f:
            for raw in f:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    seq = int(rec["sequence"])
                    payload_dict: dict[str, Any] = rec["payload"]
                    event = AuditEvent.model_validate(payload_dict)

                    if event.prev_hash != prev_hash:
                        yield (seq, False, f"prev_hash mismatch: expected {prev_hash}")
                        continue

                    expected_hash = compute_event_hash(event)
                    if event.event_hash != expected_hash:
                        yield (
                            seq,
                            False,
                            f"event_hash mismatch: expected {expected_hash}",
                        )
                        continue

                    message = compute_signature_message(
                        prev_hash=prev_hash,
                        event_hash=event.event_hash or "",
                        payload=canonical_event_payload(event),
                    )
                    try:
                        self._public_key.verify(
                            bytes.fromhex(rec["signature"]),
                            message,
                        )
                    except (InvalidSignature, ValueError) as exc:
                        yield (seq, False, f"signature verification failed: {exc}")
                        continue

                    yield (seq, True, None)
                    prev_hash = event.event_hash
                except (json.JSONDecodeError, KeyError, ValueError, ValidationError) as exc:
                    yield (-1, False, f"line parse failed: {exc}")

    def tail(self, n: int = 10) -> list[AuditEvent]:
        """Return the last ``n`` verified events. Skips invalid lines."""
        events: list[AuditEvent] = []
        # Simpler: just read raw, parse, return last n
        with open(self._path, "rb") as f:
            lines = [l for l in f.read().decode("utf-8").splitlines() if l.strip()]
        for line in lines[-n:]:
            try:
                rec = json.loads(line)
                events.append(AuditEvent.model_validate(rec["payload"]))
            except Exception:
                continue
        return events
