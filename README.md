# Adroid

> Self-hosted, contract-governed runtime that gives AI agents a stable,
> audit-grade surface to observe, request, and act on Android devices —
> with **interactive web-based permission flow** so the device owner
> approves every state-changing action.

**Status:** v0.1.0 MVP · **License:** Apache-2.0 · **Python:** ≥3.10

Adroid is not a wrapper around ADB. It is a runtime that owns the contract
and treats drivers as replaceable details. The public API survives swaps
between ADB, Termux, UIAutomator, accessibility services, Termux helpers,
and future drivers that do not exist yet.

The killer feature: **AI agents cannot do anything on your phone without
your explicit per-action approval**. Every ACT-scope call (launch app,
inject input, write file) blocks until you tap Approve in a browser
dashboard. Every call — approved or denied — is recorded in a signed,
hash-chained, append-only audit log.

## What's in v0.1.0

- ✅ Typed contract layer (Pydantic v2)
- ✅ Capability-scoped tokens (Ed25519-signed)
- ✅ Permission gate with per-tool rate limiting
- ✅ Append-only, hash-chained, Ed25519-signed audit log
- ✅ Abstract `DeviceBridge` Protocol + ADB + Mock + **Termux** implementations
- ✅ Runtime orchestrator that ties it all together
- ✅ **Interactive web permission UI** — browser dashboard, live terminal stream, per-call approve/deny
- ✅ **HTTP agent API** — AI agents call `POST /agent/call` with a token, runtime handles the rest
- ✅ **WebSocket** — live updates of pending requests + agent terminal
- ✅ Optional MCP server layer (`pip install adroid[mcp]`)

## Architecture

```
                ┌──────────────────────────────────────────┐
                │            AI Agent (HTTP client)         │
                │  POST /agent/call with CapabilityToken   │
                └──────────────────┬───────────────────────┘
                                   │
                                   ▼
                ┌──────────────────────────────────────────┐
                │            AdroidRuntime + WebServer      │
                │  ┌─────────┐  ┌─────────┐  ┌──────────┐  │
                │  │ Contract│  │  Gate   │  │  Audit   │  │
                │  │ (typed) │→ │ (scope) │→ │ (signed) │  │
                │  └─────────┘  └────┬────┘  └──────────┘  │
                │                     │ interactive         │
                │       ┌─────────────┴──────────────┐     │
                │       │  Browser Permission UI     │     │
                │       │  /ui  /ws  /pending        │     │
                │       │  /approve  /deny           │     │
                │       └────────────────────────────┘     │
                │  ┌──────────────────────────────────────┐ │
                │  │         DeviceBridge (Protocol)      │ │
                │  └──────────────────────────────────────┘ │
                └──────────────────┬───────────────────────┘
                                   │
                ┌──────────────────▼───────────────────────┐
                │       Bridge implementations             │
                │  ┌─────────┐  ┌─────────┐  ┌──────────┐ │
                │  │   ADB   │  │ Termux  │  │  Mock    │ │
                │  │(USB/net)│  │ (on phone)│ │(testing)│ │
                │  └─────────┘  └─────────┘  └──────────┘ │
                └──────────────────────────────────────────┘
```

### Permission flow

1. AI agent POSTs to `/agent/call` with a signed `CapabilityToken`.
2. Runtime validates token (signature, expiry, issuer).
3. Gate checks capability + scope + rate limit.
4. **If READ scope**: auto-approve, proceed immediately.
5. **If ACT or ADMIN scope**: create `PendingApproval`, block the call.
6. Browser dashboard shows the request (tool name, args, subject, timestamp).
7. User taps **Approve** or **Deny** → `/api/approve/{id}` or `/api/deny/{id}`.
8. Gate unblocks; runtime executes (or raises `PermissionDeniedError`).
9. Audit log records the outcome (success/error/denied) with the approver's ID.
10. WebSocket streams every step (request, approval, result, error) to the dashboard in real time.

## Install

### On a host (laptop / server / cloud)

```bash
git clone https://github.com/albytehq/adroid.git
cd adroid
pip install -e ".[web,dev,mcp]"
pytest
```

### On an Android phone (via Termux)

1. Install Termux from **F-Droid** (NOT Play Store — Play Store version is deprecated).
2. Install Termux:API from F-Droid.
3. Open Termux and run:

```bash
curl -fsSL https://raw.githubusercontent.com/albytehq/adroid/main/scripts/install_termux.sh | bash
```

Or manually:

```bash
pkg install python git termux-api curl
pip install "git+https://github.com/albytehq/adroid.git@main#egg=adroid[web,termux]"
```

## Quick start

### Mode 1: Mock (no phone needed — for dev / demo)

```bash
adroid-start --bridge mock --port 7654
```

Open `http://localhost:7654/ui` in your browser. The runtime prints a
starter agent token — use it to POST to `/agent/call`.

### Mode 2: Termux (control your phone from anywhere)

On the phone (inside Termux):

```bash
adroid-start --bridge termux --port 7654
```

In another Termux session, expose to the internet:

```bash
# Option A: cloudflared (recommended)
cloudflared tunnel --url http://localhost:7654

# Option B: ngrok
ngrok http 7654
```

Open the public URL in your phone's browser → you'll see the permission
dashboard. Give the public URL + starter token to your AI agent. Every
ACT-scope request will block until you tap Approve.

### Mode 3: ADB (laptop controlling a USB-connected phone)

```bash
# Enable USB debugging on the phone, plug it in
adb devices  # should show the device
adroid-start --bridge adb --port 7654 --adb-path $(which adb)
```

## Using the agent API

Once the runtime is running, any HTTP client can act as an AI agent:

```python
import requests

RUNTIME_URL = "https://your-ngrok-url.ngrok.app"
TOKEN_JSON = {...}  # the starter token from runtime output

# READ scope — auto-approved
res = requests.post(f"{RUNTIME_URL}/agent/call", json={
    "token": TOKEN_JSON,
    "tool": "device.list",
    "args": {},
})
devices = res.json()["result"]["devices"]

# ACT scope — blocks until the phone owner approves in browser
res = requests.post(f"{RUNTIME_URL}/agent/call", json={
    "token": TOKEN_JSON,
    "tool": "app.launch",
    "args": {
        "device_id": {"kind": "termux", "value": "self"},
        "package_name": "com.example.app",
    },
}, timeout=120)  # generous timeout for human approval
```

## Stability promise (v0.1.0)

Once v0.1.0 ships, the following are frozen until v1.0.0:

- **Capability enum values** (`device.list`, `device.observe`, etc.)
- **Scope enum values** (`read`, `act`, `admin`)
- **AuditEvent schema** — every field name and type
- **Audit hash + signature domains** (`adroid.audit.v1`, `adroid.audit.sig.v1`)
- **CapabilityToken canonical serialization** — byte-stable JSON shape
- **ToolSpec names + required args schema** for tools marked `stability="stable"`
- **Agent API endpoints** (`POST /agent/call`, `GET /api/pending`, `POST /api/approve/{id}`, etc.)

Adding new capabilities, scopes, tools, or endpoints is allowed in any
minor release. Removing or renaming is a major-release event.

## Project layout

```
adroid/
├── adroid/
│   ├── contract/        # Typed primitives (the surface)
│   ├── permissions/     # Token issuance + gate + interactive approval
│   ├── audit/           # Append-only, signed log
│   ├── bridge/          # DeviceBridge Protocol + ADB + Mock + Termux
│   ├── runtime/         # Orchestrator
│   ├── web/             # FastAPI server: permission UI + agent API + WebSocket
│   ├── mcp/             # Optional MCP server layer
│   └── cli.py           # `adroid-start` entrypoint
├── scripts/
│   ├── install_termux.sh  # Phone bootstrap
│   └── e2e_test.py        # End-to-end integration test
├── tests/               # 101 passing tests across 7 modules
├── examples/            # Runnable end-to-end demo
└── pyproject.toml
```

## Roadmap

| Version  | Months | Theme                | Key deliverables                                |
|----------|--------|----------------------|-------------------------------------------------|
| v0.1.0   | 1–4    | MVP                  | Core runtime, ADB/Termux/Mock bridges, web permission UI, 5 tools |
| v0.2.0   | 5–8    | Hardening            | Persistent ADB connection, remote blob store, OpenAPI freeze, accessibility service bridge (non-root input injection) |
| v0.3.0   | 9–12   | Scale                | Multi-replica runtime, Redis-backed rate limiter, emulator bridge |
| v0.4.0   | 13–16  | Enterprise           | SSO, audit log streaming, SLO dashboards         |
| v1.0.0   | 17–18  | GA                   | 32 MCP tools, 99.9% SLO, STRIDE-complete         |

## Contributing

v0.1.0 is founder-led. PRs welcome once we hit v0.2.0.

## License

Apache-2.0. See `LICENSE`.
