"""Adroid runtime core — the single orchestrator an integrator talks to.

The runtime wires together the four foundational layers:

    Contract  (typed surface)
    Bridge    (device I/O)
    Gate      (capability + rate-limit enforcement)
    Audit     (signed, append-only trail)

Every public method on ``AdroidRuntime`` follows the same shape:

    1. Resolve the ``Capability`` + ``Scope`` required for the operation.
    2. Run the gate (raises on denial or rate-limit).
    3. Call the bridge inside a try/except.
    4. Audit the call (success OR failure) — NEVER skip the audit.
    5. Return the value object on success; raise a typed AdroidError on failure.

The runtime is the only place that knows about all four layers. Integrators
who reach past the runtime to call the bridge directly are violating the
contract — there is no audit trail, no permission check, no rate limit.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import ValidationError

from adroid.audit.writer import AuditReader, AuditWriter
from adroid.bridge.base import BlobStore, DeviceBridge
from adroid.contract.errors import (
    AdroidError,
    AuditError,
    CapabilityNotFoundError,
    ContractError,
    DeviceNotAvailableError,
)
from adroid.contract.types import (
    AppInfo,
    AuditEvent,
    AuditOutcome,
    Capability,
    CapabilityGrant,
    CapabilityToken,
    DeviceId,
    DeviceInfo,
    Scope,
    Screenshot,
    ToolRegistry,
    ToolSpec,
)
from adroid.permissions.gate import PermissionGate
from adroid.permissions.tokens import (
    TokenIssuer,
    generate_signing_keypair,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool specs — the v0.1.0 surface, declared once and reused.
# ---------------------------------------------------------------------------


DEVICE_LIST_SPEC = ToolSpec(
    name="device.list",
    capability=Capability.DEVICE_LIST,
    required_scope=Scope.READ,
    description="List every device currently visible to the bridge.",
    args_schema={"type": "object", "properties": {}, "additionalProperties": False},
    result_schema={
        "type": "object",
        "required": ["devices"],
        "properties": {
            "devices": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["id", "state"],
                    "properties": {
                        "id": {"type": "object", "required": ["kind", "value"]},
                        "state": {"type": "string", "enum": ["online", "offline", "unauthorized", "booting", "unknown"]},
                    },
                },
            }
        },
    },
    stability="beta",
)

DEVICE_OBSERVE_SPEC = ToolSpec(
    name="device.observe",
    capability=Capability.DEVICE_OBSERVE,
    required_scope=Scope.READ,
    description="Return a fresh snapshot of one device's state + properties.",
    args_schema={
        "type": "object",
        "required": ["device_id"],
        "properties": {
            "device_id": {
                "type": "object",
                "required": ["kind", "value"],
                "properties": {
                    "kind": {"type": "string", "enum": ["adb", "emulator", "remote"]},
                    "value": {"type": "string"},
                },
            },
        },
        "additionalProperties": False,
    },
    result_schema={"type": "object", "required": ["device"]},
    stability="beta",
)

DEVICE_SCREENSHOT_SPEC = ToolSpec(
    name="device.screenshot",
    capability=Capability.DEVICE_SCREENSHOT,
    required_scope=Scope.READ,
    description="Capture a screenshot and return a content-addressed reference.",
    args_schema={
        "type": "object",
        "required": ["device_id"],
        "properties": {
            "device_id": {"type": "object", "required": ["kind", "value"]},
        },
        "additionalProperties": False,
    },
    result_schema={"type": "object", "required": ["screenshot"]},
    stability="experimental",
)

APP_LIST_SPEC = ToolSpec(
    name="app.list",
    capability=Capability.APP_LIST,
    required_scope=Scope.READ,
    description="List installed packages on a device.",
    args_schema={
        "type": "object",
        "required": ["device_id"],
        "properties": {
            "device_id": {"type": "object", "required": ["kind", "value"]},
        },
        "additionalProperties": False,
    },
    result_schema={"type": "object", "required": ["apps"]},
    stability="experimental",
)

APP_LAUNCH_SPEC = ToolSpec(
    name="app.launch",
    capability=Capability.APP_LAUNCH,
    required_scope=Scope.ACT,
    description="Launch an app by package name.",
    args_schema={
        "type": "object",
        "required": ["device_id", "package_name"],
        "properties": {
            "device_id": {"type": "object", "required": ["kind", "value"]},
            "package_name": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "additionalProperties": False,
    },
    result_schema={"type": "object", "required": []},
    stability="experimental",
)


_V0_TOOLS: list[ToolSpec] = [
    DEVICE_LIST_SPEC,
    DEVICE_OBSERVE_SPEC,
    DEVICE_SCREENSHOT_SPEC,
    APP_LIST_SPEC,
    APP_LAUNCH_SPEC,
]


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class AdroidRuntime:
    """The single orchestrator. Construct one per process.

    Lifecycle:
        - Boot: generate or load signing keys, open audit log.
        - Steady state: serve tool calls.
        - Shutdown: flush audit writer (no-op for v0.1.0 file backend).

    The runtime is thread-safe for tool calls. It is NOT safe to call
    ``shutdown`` while another thread is mid-call — use a real coordinator.
    """

    def __init__(
        self,
        *,
        issuer_id: str | None = None,
        bridge: DeviceBridge,
        blob_store: BlobStore,
        audit_log_path: Path,
        token_signing_key: Ed25519PrivateKey | None = None,
        token_public_key: Ed25519PublicKey | None = None,
        audit_signing_key: Ed25519PrivateKey | None = None,
        audit_public_key: Ed25519PublicKey | None = None,
    ):
        self._issuer_id = issuer_id or f"adroid-{secrets.token_hex(4)}"
        self._bridge = bridge
        self._blob_store = blob_store

        # Token-issuance keypair (separate from audit keypair by policy).
        if token_signing_key is not None and token_public_key is not None:
            self._token_private = token_signing_key
            self._token_public = token_public_key
        else:
            self._token_private, self._token_public = generate_signing_keypair()

        # Audit signing keypair (separate).
        if audit_signing_key is not None and audit_public_key is not None:
            self._audit_private = audit_signing_key
            self._audit_public = audit_public_key
        else:
            self._audit_private, self._audit_public = generate_signing_keypair()

        self._issuer = TokenIssuer(
            issuer_id=self._issuer_id,
            signing_key=self._token_private,
            public_key=self._token_public,
        )
        self._gate = PermissionGate(
            issuer_id=self._issuer_id,
            public_key=self._token_public,
        )
        self._audit = AuditWriter(
            log_path=audit_log_path,
            signing_key=self._audit_private,
            public_key=self._audit_public,
        )

        # Tool registry: name -> spec
        self._tools: dict[str, ToolSpec] = {t.name: t for t in _V0_TOOLS}

        # Boot event — first thing in every audit log.
        self._audit_boot()

    # ------------------------------------------------------------------
    # Identity / inspection
    # ------------------------------------------------------------------

    @property
    def issuer_id(self) -> str:
        return self._issuer_id

    @property
    def token_public_key_pem(self) -> str:
        """PEM for the token-issuance public key. Ship this to verifiers."""
        from adroid.permissions.tokens import serialize_public_key
        return serialize_public_key(self._token_public)

    @property
    def audit_public_key_pem(self) -> str:
        """PEM for the audit-signing public key. Ship this to log verifiers."""
        from adroid.permissions.tokens import serialize_public_key
        return serialize_public_key(self._audit_public)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def get_tool(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    # ------------------------------------------------------------------
    # Token issuance (admin-side)
    # ------------------------------------------------------------------

    def issue_token(
        self,
        *,
        subject: str,
        grants: Iterable[CapabilityGrant | tuple[Capability, Scope]],
        ttl_seconds: int = 3600,
    ) -> CapabilityToken:
        """Mint a signed capability token for an integrator / agent."""
        from datetime import timedelta
        return self._issuer.issue(
            subject=subject,
            grants=grants,
            ttl=timedelta(seconds=ttl_seconds),
        )

    # ------------------------------------------------------------------
    # Tool calls (integrator-side)
    # ------------------------------------------------------------------

    def list_devices(self, *, token: CapabilityToken) -> list[DeviceInfo]:
        spec = self._require_tool("device.list")
        self._gate.authorize(token, spec.capability, spec.required_scope)
        try:
            result = self._bridge.list_devices()
        except AdroidError as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code=exc.code, args={})
            raise
        except Exception as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code="adroid.unexpected", args={}, error=str(exc))
            raise ContractError(f"unexpected bridge error: {exc}", code="adroid.unexpected") from exc
        self._audit_call(token, spec, outcome=AuditOutcome.SUCCESS, args={}, result_count=len(result))
        return result

    def observe_device(self, *, token: CapabilityToken, device_id: DeviceId) -> DeviceInfo:
        spec = self._require_tool("device.observe")
        self._gate.authorize(token, spec.capability, spec.required_scope)
        args = {"device_id": str(device_id)}
        try:
            result = self._bridge.get_device_info(device_id)
        except AdroidError as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code=exc.code, args=args, device_id=device_id)
            raise
        except Exception as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code="adroid.unexpected", args=args, device_id=device_id, error=str(exc))
            raise ContractError(f"unexpected bridge error: {exc}", code="adroid.unexpected") from exc
        self._audit_call(token, spec, outcome=AuditOutcome.SUCCESS, args=args, device_id=device_id)
        return result

    def capture_screenshot(self, *, token: CapabilityToken, device_id: DeviceId) -> Screenshot:
        spec = self._require_tool("device.screenshot")
        self._gate.authorize(token, spec.capability, spec.required_scope)
        args = {"device_id": str(device_id)}
        try:
            result = self._bridge.capture_screenshot(device_id)
        except AdroidError as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code=exc.code, args=args, device_id=device_id)
            raise
        except Exception as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code="adroid.unexpected", args=args, device_id=device_id, error=str(exc))
            raise ContractError(f"unexpected bridge error: {exc}", code="adroid.unexpected") from exc
        self._audit_call(
            token,
            spec,
            outcome=AuditOutcome.SUCCESS,
            args=args,
            device_id=device_id,
            extra_metadata={"blob_ref": result.blob_ref, "width": result.width, "height": result.height},
        )
        return result

    def list_installed_apps(self, *, token: CapabilityToken, device_id: DeviceId) -> list[AppInfo]:
        spec = self._require_tool("app.list")
        self._gate.authorize(token, spec.capability, spec.required_scope)
        args = {"device_id": str(device_id)}
        try:
            result = self._bridge.list_installed_apps(device_id)
        except AdroidError as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code=exc.code, args=args, device_id=device_id)
            raise
        except Exception as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code="adroid.unexpected", args=args, device_id=device_id, error=str(exc))
            raise ContractError(f"unexpected bridge error: {exc}", code="adroid.unexpected") from exc
        self._audit_call(token, spec, outcome=AuditOutcome.SUCCESS, args=args, device_id=device_id, result_count=len(result))
        return result

    def launch_app(self, *, token: CapabilityToken, device_id: DeviceId, package_name: str) -> None:
        spec = self._require_tool("app.launch")
        self._gate.authorize(token, spec.capability, spec.required_scope)
        args = {"device_id": str(device_id), "package_name": package_name}
        try:
            self._bridge.launch_app(device_id, package_name)
        except AdroidError as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code=exc.code, args=args, device_id=device_id)
            raise
        except Exception as exc:
            self._audit_call(token, spec, outcome=AuditOutcome.ERROR, error_code="adroid.unexpected", args=args, device_id=device_id, error=str(exc))
            raise ContractError(f"unexpected bridge error: {exc}", code="adroid.unexpected") from exc
        self._audit_call(token, spec, outcome=AuditOutcome.SUCCESS, args=args, device_id=device_id)

    def get_blob(self, blob_ref: str) -> bytes | None:
        """Resolve a content-addressed reference to its bytes.

        Public so integrators can fetch screenshot bytes after a
        ``device.screenshot`` call. No token required — blob_refs are
        unguessable 256-bit hashes; possession == authorization.
        """
        return self._blob_store.get(blob_ref)

    # ------------------------------------------------------------------
    # Audit reader (admin-side)
    # ------------------------------------------------------------------

    def audit_reader(self) -> AuditReader:
        """Return a fresh AuditReader bound to this runtime's audit log."""
        return AuditReader(
            log_path=self._audit._path,  # noqa: SLF001 — same-package access
            public_key=self._audit_public,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _require_tool(self, name: str) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None:
            raise CapabilityNotFoundError(
                f"tool not registered: {name}",
                code="adroid.tool.not_registered",
                details={"tool_name": name},
            )
        return spec

    def _audit_call(
        self,
        token: CapabilityToken,
        spec: ToolSpec,
        *,
        outcome: AuditOutcome,
        args: dict[str, Any],
        device_id: DeviceId | None = None,
        error_code: str | None = None,
        result_count: int | None = None,
        extra_metadata: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Write one audit event. NEVER raises — audit failures are logged
        but never break a tool call. (v0.2.0 will add a strict mode.)"""
        args_digest = hashlib.sha256(
            json.dumps(args, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

        metadata: dict[str, Any] = {}
        if result_count is not None:
            metadata["result_count"] = result_count
        if extra_metadata:
            metadata.update(extra_metadata)
        if error:
            metadata["error"] = error[:1024]

        event = AuditEvent(
            token_id=token.token_id,
            subject=token.subject,
            capability=spec.capability,
            scope=spec.required_scope,
            outcome=outcome,
            error_code=error_code,
            device_id=device_id,
            method=f"runtime.{spec.name.replace('.', '_')}",
            args_digest=args_digest,
            metadata=metadata,
        )
        try:
            self._audit.write(event)
        except AuditError as exc:
            # Don't let audit failures break the call in v0.1.0. Log loudly.
            log.error(
                "audit write failed (outcome=%s, method=%s, error_code=%s): %s",
                outcome.value,
                event.method,
                error_code,
                exc,
            )

    def _audit_boot(self) -> None:
        """Write a system boot event to the audit log. No token."""
        event = AuditEvent(
            token_id=None,
            subject=None,
            capability=None,
            scope=None,
            outcome=AuditOutcome.SUCCESS,
            method="runtime.boot",
            args_digest=hashlib.sha256(b"").hexdigest(),
            metadata={"issuer_id": self._issuer_id, "version": "0.1.0"},
        )
        try:
            self._audit.write(event)
        except AuditError as exc:
            log.error("audit boot event failed: %s", exc)


# ---------------------------------------------------------------------------
# Convenience: build a runtime from a config dict (for scripts / tests)
# ---------------------------------------------------------------------------


def build_runtime(
    *,
    bridge: DeviceBridge,
    blob_store: BlobStore,
    audit_log_path: Path,
    issuer_id: str | None = None,
) -> AdroidRuntime:
    """One-shot constructor for the common case.

    Generates fresh keypairs each call. For production, persist keys via
    your secret manager and pass them into ``AdroidRuntime`` directly.
    """
    return AdroidRuntime(
        issuer_id=issuer_id,
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=audit_log_path,
    )


# Make sure ToolRegistry Protocol is satisfied structurally.
_is_registry: ToolRegistry = AdroidRuntime  # type: ignore[assignment]
