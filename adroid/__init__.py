"""Adroid — contract-governed runtime for AI agents on Android devices.

Public surface (v0.1.0 MVP):
    - adroid.contract   — typed primitives that define the runtime surface
    - adroid.permissions — capability-scoped tokens and gate enforcement
    - adroid.audit      — append-only, Ed25519-signed audit trail
    - adroid.bridge     — abstract DeviceBridge Protocol + ADB implementation
    - adroid.runtime    — Core runtime that ties bridge + gate + audit together

The runtime is transport-agnostic. MCP server lives in adroid.mcp and is an
optional dependency (install with `pip install adroid[mcp]`).
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
