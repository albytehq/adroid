# Adroid

> Self-hosted, contract-governed runtime that gives AI agents a stable,
> audit-grade surface to observe, request, and act on Android devices.

**Status:** v0.1.0 MVP (in development) · **License:** Apache-2.0 · **Python:** ≥3.10

Adroid is not a wrapper around ADB. It is a runtime that owns the contract
and treats drivers as replaceable details. The public API survives swaps
between ADB, UIAutomator, accessibility services, Termux helpers, and
future drivers that do not exist yet.

This repository contains the **core runtime** — the foundation that the
18-month roadmap (`docs/roadmap.pdf`) builds on top of. v0.1.0 ships:

- ✅ Typed contract layer (Pydantic v2)
- ✅ Capability-scoped tokens (Ed25519-signed)
- ✅ Permission gate with per-tool rate limiting
- ✅ Append-only, hash-chained, Ed25519-signed audit log
- ✅ Abstract `DeviceBridge` Protocol + ADB implementation + Mock implementation
- ✅ Runtime orchestrator that ties it all together
- ✅ Optional MCP server layer (`pip install adroid[mcp]`)

## Architecture

```
                ┌──────────────────────────────────────────┐
                │            AI Agent / Integrator          │
                └──────────────────┬───────────────────────┘
                                   │  tool call + CapabilityToken
                                   ▼
                ┌──────────────────────────────────────────┐
                │            AdroidRuntime                  │
                │  ┌─────────┐  ┌─────────┐  ┌──────────┐  │
                │  │ Contract│  │  Gate   │  │  Audit   │  │
                │  │ (typed) │→ │ (scope) │→ │ (signed) │  │
                │  └─────────┘  └─────────┘  └──────────┘  │
                │       │            │            │         │
                │       ▼            ▼            ▼         │
                │  ┌──────────────────────────────────────┐ │
                │  │         DeviceBridge (Protocol)      │ │
                │  └──────────────────────────────────────┘ │
                └──────────────────┬───────────────────────┘
                                   │
                ┌──────────────────▼───────────────────────┐
                │       Bridge implementations             │
                │  ┌─────────┐  ┌─────────┐  ┌──────────┐ │
                │  │   ADB   │  │  Mock   │  │  Future  │ │
                │  └─────────┘  └─────────┘  └──────────┘ │
                └──────────────────────────────────────────┘
```

Every tool call flows through four layers in order:
1. **Contract** — typed value objects validate input shape.
2. **Gate** — verifies the token signature, checks capability + scope, enforces rate limits.
3. **Bridge** — performs the actual device I/O.
4. **Audit** — writes a signed, hash-chained event recording the outcome.

If the gate denies, the bridge is never touched. If the bridge errors, the
audit still records the failure with the typed error code. There is no path
from an integrator to a device that skips any layer.

## Install (dev)

```bash
git clone https://github.com/albyteECO/adroid.git
cd adroid
pip install -e ".[dev,mcp]"
pytest
```

## Quick start

```python
from pathlib import Path
from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.types import Capability, DeviceId, DeviceInfo, DeviceState, Scope
from adroid.runtime.core import AdroidRuntime

# 1. Boot the runtime
blob_store = InMemoryBlobStore()
bridge = MockBridge(blob_store=blob_store)
bridge.add_device(
    DeviceInfo(
        id=DeviceId(kind="adb", value="mock-1"),
        state=DeviceState.ONLINE,
        model="MockPhone",
    )
)
runtime = AdroidRuntime(
    bridge=bridge,
    blob_store=blob_store,
    audit_log_path=Path("/tmp/adroid.auditlog"),
)

# 2. Issue a token for your agent
token = runtime.issue_token(
    subject="agent:foo",
    grants=[(Capability.DEVICE_LIST, Scope.READ)],
)

# 3. Call tools — every call is gated + audited
for device in runtime.list_devices(token=token):
    print(device.id, device.state)
```

Run the full end-to-end example:

```bash
python examples/observe_device.py
```

## Run as an MCP server

```bash
# 1. Generate a capability token (one-time per agent)
python -m adroid.mcp.issue_token \
    --audit-log /tmp/adroid.auditlog \
    --subject "agent:foo" \
    --grant device.list:read \
    --grant device.observe:read \
    --print-env

# 2. Source the env var
eval "$(python -m adroid.mcp.issue_token ... --print-env 2>&1 | grep export)"

# 3. Run the MCP server (mock mode for demo, no device needed)
python -m adroid.mcp.server --audit-log /tmp/adroid.auditlog --mock
```

Point any MCP-capable client (Claude Desktop, etc.) at the server process
via stdio transport.

## Stability promise (v0.1.0)

Once v0.1.0 ships, the following are frozen until v1.0.0:

- **Capability enum values** (`device.list`, `device.observe`, etc.)
- **Scope enum values** (`read`, `act`, `admin`)
- **AuditEvent schema** — every field name and type
- **Audit hash + signature domains** (`adroid.audit.v1`, `adroid.audit.sig.v1`)
- **CapabilityToken canonical serialization** — byte-stable JSON shape
- **ToolSpec names + required args schema** for tools marked `stability="stable"`

Adding new capabilities, scopes, or tools is allowed in any minor release.
Removing or renaming is a major-release event.

## Project layout

```
adroid/
├── adroid/
│   ├── contract/        # Typed primitives (the surface)
│   ├── permissions/     # Token issuance + gate
│   ├── audit/           # Append-only, signed log
│   ├── bridge/          # DeviceBridge Protocol + ADB + Mock
│   ├── runtime/         # Orchestrator
│   └── mcp/             # Optional MCP server layer
├── tests/               # pytest suite — one file per layer
├── examples/            # Runnable end-to-end demo
└── pyproject.toml
```

## Roadmap

See `docs/roadmap.pdf` for the full 18-month engineering spec covering
v0.1.0 → v1.0.0. Highlights:

| Version  | Months | Theme                | Key deliverables                                |
|----------|--------|----------------------|-------------------------------------------------|
| v0.1.0   | 1–4    | MVP                  | Core runtime, ADB bridge, 5 MCP tools            |
| v0.2.0   | 5–8    | Hardening            | Persistent ADB connection, remote blob store, OpenAPI freeze |
| v0.3.0   | 9–12   | Scale                | Multi-replica runtime, Redis-backed rate limiter, emulator bridge |
| v0.4.0   | 13–16  | Enterprise           | SSO, audit log streaming, SLO dashboards         |
| v1.0.0   | 17–18  | GA                   | 32 MCP tools, 99.9% SLO, STRIDE-complete         |

## Contributing

v0.1.0 is founder-led. PRs welcome once we hit v0.2.0. See
`docs/roadmap.pdf` for what's in scope per version.

## License

Apache-2.0. See `LICENSE`.
