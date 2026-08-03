"""Adroid — contract-governed runtime for AI agents on Android devices.

Public surface:
    - adroid.contract    — typed primitives that define the runtime surface
    - adroid.permissions — capability-scoped tokens and gate enforcement
    - adroid.audit       — append-only, Ed25519-signed audit trail
    - adroid.bridge      — abstract DeviceBridge Protocol + ADB/Mock/Termux
    - adroid.runtime     — Core runtime that ties bridge + gate + audit together
    - adroid.perception  — confidence scoring + self-healing selectors
    - adroid.memory      — persistent device/skill/failure memory

The runtime is transport-agnostic. MCP server lives in adroid.mcp and is an
optional dependency (install with `pip install adroid[mcp]`).
"""

__version__ = "0.3.6"
__all__ = ["__version__"]
