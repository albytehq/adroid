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
    CAPABILITY_TO_SCOPE,
    Capability,
    CapabilityGrant,
    CapabilityToken,
    DeviceId,
    DeviceInfo,
    DeviceState,
    PermissionScope,
    Screenshot,
    Scope,
    ToolRegistry,
    ToolSpec,
)
from adroid.contract.ui import (
    Bounds,
    NodeRole,
    SemanticNode,
    Selector,
    TapElementResult,
    WaitConditionType,
    WaitForCondition,
    WaitForResult,
    WorldState,
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
    "NodeRole",
    "PermissionScope",
    "Scope",
    "WaitConditionType",
    # Value objects
    "AppInfo",
    "AuditEvent",
    "Bounds",
    "CAPABILITY_TO_SCOPE",
    "CapabilityGrant",
    "CapabilityToken",
    "DeviceId",
    "DeviceInfo",
    "Screenshot",
    "Selector",
    "SemanticNode",
    "TapElementResult",
    "WaitForCondition",
    "WaitForResult",
    "WorldState",
    # Tool registry
    "ToolRegistry",
    "ToolSpec",
]
