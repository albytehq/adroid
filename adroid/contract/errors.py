"""Contract errors for the Adroid runtime.

These are the only exceptions that cross the public API boundary.
All other exceptions are wrapped into one of these by the runtime layer.
"""

from __future__ import annotations


class AdroidError(Exception):
    """Base class for every Adroid public-API error.

    Every contract error carries a stable ``code`` string. AI agents and
    integrators may branch on ``code`` rather than parsing the message.
    Codes are part of the stability promise and never change within a minor
    version.
    """

    code: str = "adroid.error"
    http_status: int = 500

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.details = details or {}


class ContractError(AdroidError):
    """A call violated the typed contract (bad input, wrong shape, etc.)."""

    code = "adroid.contract.violation"
    http_status = 400


class PermissionDeniedError(AdroidError):
    """The capability token does not grant the requested scope."""

    code = "adroid.permission.denied"
    http_status = 403


class CapabilityNotFoundError(AdroidError):
    """The requested capability is not registered with the runtime."""

    code = "adroid.capability.not_found"
    http_status = 404


class DeviceNotAvailableError(AdroidError):
    """The target device is offline, unpaired, or does not exist."""

    code = "adroid.device.not_available"
    http_status = 503


class BridgeError(AdroidError):
    """The device bridge returned an unrecoverable error."""

    code = "adroid.bridge.error"
    http_status = 502


class AuditError(AdroidError):
    """The audit writer rejected an event (tamper, I/O, signature failure)."""

    code = "adroid.audit.error"
    http_status = 500


class RateLimitedError(AdroidError):
    """The call exceeded the bounded resource quota for this token."""

    code = "adroid.rate_limited"
    http_status = 429
