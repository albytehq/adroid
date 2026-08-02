"""Contract layer — typed primitives that define the runtime surface.

This package is the ONLY thing an AI implementer needs to import to understand
the contract. Everything else (permissions, audit, bridge, runtime) builds on
top of these types.
"""

from adroid.contract.errors import (
    AdroidError,
    AuditError,
    BridgeError,
    CapabilityNotFoundError,
    ContractError,
    DeviceNotAvailableError,
    PermissionDeniedError,
    RateLimitedError,
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
    DeviceState,
    Screenshot,
    Scope,
    ToolRegistry,
    ToolSpec,
)

__all__ = [
    # Errors
    "AdroidError",
    "AuditError",
    "BridgeError",
    "CapabilityNotFoundError",
    "ContractError",
    "DeviceNotAvailableError",
    "PermissionDeniedError",
    "RateLimitedError",
    # Enums
    "AuditOutcome",
    "Capability",
    "DeviceState",
    "Scope",
    # Value objects
    "AppInfo",
    "AuditEvent",
    "CapabilityGrant",
    "CapabilityToken",
    "DeviceId",
    "DeviceInfo",
    "Screenshot",
    # Tool registry
    "ToolRegistry",
    "ToolSpec",
]
