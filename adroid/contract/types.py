"""Core typed primitives that define the Adroid runtime surface.

These types ARE the contract. Once v0.1.0 ships, every field rename, type
change, or removal here is a breaking change that requires a minor-version
bump and a migration note in CHANGELOG.

Design rules:
    - Every public type is a Pydantic v2 model — gives us validation,
      JSON-schema export (for OpenAPI / MCP tool schema), and stable
      serialization in one shot.
    - Every type carries a ``schema_version`` field where it makes sense,
      so audit logs can be replayed against the schema that produced them.
    - ``frozen=True`` on value objects — once observed, the data does not
      mutate. This is essential for audit-grade replay.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Enums — closed vocabularies that AI agents can branch on
# ---------------------------------------------------------------------------


class Capability(str, Enum):
    """Closed set of capabilities a token may carry.

    Naming convention: ``<domain>.<verb>``. Domains today are ``device``,
    ``app``, ``input``, ``fs``, ``net``. New capabilities must be added via
    a minor-version bump and announced in CHANGELOG.
    """

    # Device observation (read-only)
    DEVICE_LIST = "device.list"
    DEVICE_OBSERVE = "device.observe"
    DEVICE_SCREENSHOT = "device.screenshot"

    # App lifecycle
    APP_LIST = "app.list"
    APP_LAUNCH = "app.launch"
    APP_FORCE_STOP = "app.force_stop"

    # Input injection (write-side — requires elevated scope)
    INPUT_TAP = "input.tap"
    INPUT_TAP_TEXT = "input.tap_text"
    INPUT_SWIPE = "input.swipe"
    INPUT_TEXT = "input.text"
    INPUT_KEY = "input.key"

    # Shell execution (allowlisted — see adroid.permissions.allowlist)
    SHELL_EXEC = "shell.exec"

    # Log observation (read-only)
    LOGS_READ = "logs.read"

    # Permission meta-tool (request a new scope mid-session)
    PERMISSION_REQUEST = "permission.request"

    # v0.2.0 semantic UI tools
    UI_DUMP = "ui.dump"
    UI_TAP_ELEMENT = "ui.tap_element"
    UI_WAIT_FOR = "ui.wait_for"
    UI_FIND_ELEMENTS = "ui.find_elements"

    # Filesystem (sandboxed to package dirs)
    FS_READ = "fs.read"
    FS_WRITE = "fs.write"

    # Network observation (read-only)
    NET_OBSERVE = "net.observe"


class PermissionScope(str, Enum):
    """Permission scope taxonomy matching the v0.1.0 roadmap spec (Table 5.2).

    Each tool maps to exactly one permission scope. The bootstrap flow
    grants ALL scopes at ACT tier. Future versions (v1.0.0 RBAC) will
    allow per-scope delegation.

    These are stable strings — AI agents may branch on them. Do NOT
    rename without a major version bump.
    """

    NONE = "none"
    OBSERVE = "observe"
    SCREEN_READ = "screen_read"
    TOUCH_CONTROL = "touch_control"
    KEYBOARD_INPUT = "keyboard_input"
    SHELL_EXEC = "shell_exec"
    NOTIFICATIONS_READ = "notifications_read"
    UI_INSPECT = "ui_inspect"


# Mapping: Capability → required PermissionScope
# This is the source of truth for the spec's "Permission Scope" column.
CAPABILITY_TO_SCOPE: dict[Capability, PermissionScope] = {
    Capability.DEVICE_LIST: PermissionScope.NONE,
    Capability.DEVICE_OBSERVE: PermissionScope.OBSERVE,
    Capability.DEVICE_SCREENSHOT: PermissionScope.SCREEN_READ,
    Capability.APP_LIST: PermissionScope.NONE,
    Capability.APP_LAUNCH: PermissionScope.TOUCH_CONTROL,
    Capability.APP_FORCE_STOP: PermissionScope.TOUCH_CONTROL,
    Capability.INPUT_TAP: PermissionScope.TOUCH_CONTROL,
    Capability.INPUT_TAP_TEXT: PermissionScope.TOUCH_CONTROL,
    Capability.INPUT_SWIPE: PermissionScope.TOUCH_CONTROL,
    Capability.INPUT_TEXT: PermissionScope.KEYBOARD_INPUT,
    Capability.INPUT_KEY: PermissionScope.TOUCH_CONTROL,
    Capability.SHELL_EXEC: PermissionScope.SHELL_EXEC,
    Capability.LOGS_READ: PermissionScope.NOTIFICATIONS_READ,
    Capability.PERMISSION_REQUEST: PermissionScope.NONE,
    Capability.UI_DUMP: PermissionScope.UI_INSPECT,
    Capability.UI_TAP_ELEMENT: PermissionScope.TOUCH_CONTROL,
    Capability.UI_WAIT_FOR: PermissionScope.UI_INSPECT,
    Capability.UI_FIND_ELEMENTS: PermissionScope.UI_INSPECT,
    Capability.FS_READ: PermissionScope.SHELL_EXEC,
    Capability.FS_WRITE: PermissionScope.SHELL_EXEC,
    Capability.NET_OBSERVE: PermissionScope.OBSERVE,
}


class Scope(str, Enum):
    """Privilege tier for a capability.

    ``READ``  — observation only. Safe for untrusted agents.
    ``ACT``   — state-changing actions on device (taps, app launches).
    ``ADMIN`` — bridge-level operations (pair, unpair, reboot). Audit-strict.
    """

    READ = "read"
    ACT = "act"
    ADMIN = "admin"


class AuditOutcome(str, Enum):
    """Did the audited call succeed or fail? Required on every AuditEvent."""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class DeviceState(str, Enum):
    """State of a device as observed by the bridge."""

    ONLINE = "online"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    BOOTING = "booting"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Value objects — frozen, validated, JSON-serializable
# ---------------------------------------------------------------------------


class _Frozen(BaseModel):
    """Base for every value object that crosses the public API.

    Frozen == hashable-ish and safe to log, replay, and cache.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


class DeviceId(_Frozen):
    """Stable identifier for a device bridge target.

    ``kind`` is the bridge family (``adb``, ``emulator``, ``remote``).
    ``value`` is the bridge-native serial (e.g. ADB serial, emulator port).
    """

    kind: Literal["adb", "emulator", "remote", "termux"] = "adb"
    value: str = Field(min_length=1, max_length=128)

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


class DeviceInfo(_Frozen):
    """Snapshot of a device as seen by the bridge at observation time."""

    id: DeviceId
    state: DeviceState
    model: str | None = None
    manufacturer: str | None = None
    android_version: str | None = None
    sdk_level: int | None = None
    battery_level: float | None = Field(default=None, ge=0.0, le=100.0)
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("observed_at")
    @classmethod
    def _must_be_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware (UTC)")
        return v.astimezone(timezone.utc)


class Screenshot(_Frozen):
    """A captured screenshot. Bytes are NOT stored in the model — the
    ``blob_ref`` is a content-addressed reference the runtime can resolve.

    For v0.1.0 the reference is a sha256 hex string. v0.2.0 may switch to
    a URI when remote storage lands.
    """

    device_id: DeviceId
    blob_ref: str = Field(pattern=r"^[0-9a-f]{64}$")  # sha256
    width: int = Field(gt=0, le=8192)
    height: int = Field(gt=0, le=8192)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class AppInfo(_Frozen):
    """A installed package observed on a device."""

    package_name: str = Field(min_length=1, max_length=256)
    version_name: str | None = None
    version_code: int | None = None
    system_app: bool = False
    enabled: bool = True


# ---------------------------------------------------------------------------
# Capability token — issued by the runtime, consumed by the gate
# ---------------------------------------------------------------------------


class CapabilityGrant(_Frozen):
    """One entry inside a CapabilityToken. Grants a capability at a scope."""

    capability: Capability
    scope: Scope
    # Optional resource quota. None means "use runtime default".
    rate_limit_per_minute: int | None = Field(default=None, ge=0, le=10_000)


class CapabilityToken(_Frozen):
    """Signed token presented on every tool call.

    The token is signed by the runtime's Ed25519 issuer key. The signature
    is detached — see adroid.permissions.tokens for sign/verify logic.

    ``token_id`` is a UUID v4 generated at issuance time. It is the primary
    key in the audit log, so every audited call can be traced back to the
    exact issuance event.
    """

    token_id: UUID = Field(default_factory=uuid4)
    issuer: str = Field(min_length=1, max_length=64)  # runtime instance id
    subject: str = Field(min_length=1, max_length=128)  # agent / integrator id
    issued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime
    grants: frozenset[CapabilityGrant] = Field(default_factory=frozenset)
    # Detached Ed25519 signature over the canonical JSON of every other field.
    # Hex-encoded, 128 chars.
    signature: str = Field(pattern=r"^[0-9a-f]{128}$")

    @field_validator("expires_at")
    @classmethod
    def _expires_after_issued(cls, v: datetime, info) -> datetime:
        issued = info.data.get("issued_at")
        if issued and v <= issued:
            raise ValueError("expires_at must be strictly after issued_at")
        return v

    def has(self, capability: Capability, scope: Scope | None = None) -> bool:
        """Does this token grant ``capability`` (optionally at >= ``scope``)?"""
        for g in self.grants:
            if g.capability != capability:
                continue
            if scope is None:
                return True
            if _scope_rank(g.scope) >= _scope_rank(scope):
                return True
        return False


def _scope_rank(scope: Scope) -> int:
    """READ < ACT < ADMIN. Used for hierarchical scope checks."""
    return {Scope.READ: 0, Scope.ACT: 1, Scope.ADMIN: 2}[scope]


# ---------------------------------------------------------------------------
# Audit event — the unit that hits the append-only log
# ---------------------------------------------------------------------------


class AuditEvent(_Frozen):
    """One row in the audit log. Immutable once written.

    The audit writer computes ``event_hash`` and ``prev_hash`` to form a
    hash chain. Signature is over (prev_hash || event_hash || payload).
    """

    sequence: int = Field(default=0, ge=0)  # filled by writer; 0 until written
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Who / what
    token_id: UUID | None = None  # None for system events (boot, key rotation)
    subject: str | None = None
    capability: Capability | None = None
    scope: Scope | None = None

    # Outcome
    outcome: AuditOutcome
    error_code: str | None = None  # AdroidError.code if outcome != SUCCESS

    # What was called
    device_id: DeviceId | None = None
    method: str  # e.g. "runtime.observe_device"
    args_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )  # sha256 of canonical args JSON

    # Chain fields (filled by writer)
    prev_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    # Optional free-form metadata (size-capped; writer enforces)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Capability registry Protocol — what the runtime exposes to integrators
# ---------------------------------------------------------------------------


class ToolSpec(_Frozen):
    """Declarative spec for a runtime-exposed tool.

    Used to auto-generate MCP tool schemas and OpenAPI operation docs.
    """

    name: str  # e.g. "device.observe"
    capability: Capability
    required_scope: Scope
    description: str
    # JSON schema fragment for the args object (Draft 2020-12).
    args_schema: dict[str, Any]
    # JSON schema fragment for the result object.
    result_schema: dict[str, Any]
    # Stability flag — once ``stable``, the schema is frozen for the minor.
    stability: Literal["experimental", "beta", "stable"] = "experimental"


class ToolRegistry(Protocol):
    """Structural interface every runtime must implement."""

    def list_tools(self) -> list[ToolSpec]: ...

    def get_tool(self, name: str) -> ToolSpec | None: ...
