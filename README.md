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
                │         AI Agent (HTTP client)           │
                │   any chat agent / script / IDE plugin   │
                └──────────────────┬───────────────────────┘
                                   │
                                   │ POST /agent/call/async
                                   │   { token, tool, args }
                                   ▼
                ┌──────────────────────────────────────────┐
                │      LAPTOP (server + monitor UI)        │
                │  ┌────────────────────────────────────┐  │
                │  │  Adroid Runtime (Python)           │  │
                │  │  ┌──────────┐  ┌────────────────┐  │  │
                │  │  │ Contract │  │ Audit Log      │  │  │
                │  │  │ (typed)  │  │ (signed chain) │  │  │
                │  │  └──────────┘  └────────────────┘  │  │
                │  │  ┌────────────────────────────────┐ │  │
                │  │  │ InteractiveGate                │ │  │
                │  │  │  ├── READ scope: auto-approve  │ │  │
                │  │  │  └── ACT/ADMIN: block + URL    │ │  │
                │  │  └────────────────────────────────┘ │  │
                │  │  ┌────────────────────────────────┐ │  │
                │  │  │ AdbBridge → ADB → USB/WiFi     │ │  │
                │  │  └────────────────────────────────┘ │  │
                │  └────────────────────────────────────┘  │
                │                                          │
                │  Browser (your laptop):                  │
                │    /ui  →  approve/deny + live terminal  │
                │             + screenshot preview         │
                └──────────────────┬───────────────────────┘
                                   │
                                   │ ADB (USB cable or WiFi)
                                   ▼
                ┌──────────────────────────────────────────┐
                │           ANDROID PHONE (target)         │
                │  - USB Debugging ON                      │
                │  - Tap "Allow" on RSA prompt (1x)        │
                │  - No Adroid software on the phone       │
                └──────────────────────────────────────────┘
```

### Async agent flow (the main use case)

For chat-based AI agents (like the one talking to you right now) that
can't hold long-lived HTTP connections:

1. **Agent** POSTs to `/agent/call/async` with `{token, tool, args}`.
2. **Server** validates token + scope + rate limit.
   - READ scope → executes immediately, returns `{status: "completed", result: {...}}`.
   - ACT/ADMIN scope → registers pending approval, returns `{status: "pending_approval", approval_id, approval_url}`.
3. **Agent** shares the `approval_url` with the human (via chat, email, whatever).
4. **Human** opens `approval_url` in browser → dashboard scrolls to + highlights the pending card.
5. **Human** taps **Approve** or **Deny**.
6. **Agent** polls `/agent/result/{approval_id}` every 1–3 seconds.
   - `state: "pending"` → keep polling
   - `state: "completed"` → read `result`, continue
   - `state: "denied"` / `"timeout"` / `"error"` → handle gracefully
7. **Audit log** records every step (request, approval, execution, result).

This is the flow we just used to test — and it works. The agent never
holds a connection open; the human can take as long as they want to
approve; everything is signed and replayable.

## Install

### On a laptop (the primary mode)

```bash
git clone https://github.com/albytehq/adroid.git
cd adroid
pip install -e ".[web,dev,mcp]"
pytest
```

You'll also need the `adb` binary:

- Debian/Ubuntu: `sudo apt install android-tools-adb`
- macOS: `brew install android-tools`
- Windows: download platform-tools from developer.android.com

### On an Android phone (alternative Termux mode — runtime runs ON the phone)

1. Install Termux from **F-Droid** (NOT Play Store — Play Store version is deprecated).
2. Install Termux:API from F-Droid.
3. Open Termux and run:

```bash
curl -fsSL https://raw.githubusercontent.com/albytehq/adroid/main/scripts/install_termux.sh | bash
```

This mode is useful if you don't have a laptop or want the runtime to live on the phone permanently. The primary mode (laptop + ADB) is recommended — easier to monitor, more powerful (full ADB control including input injection), and the audit log stays on a machine you control.

## Quick start

### Step 1: Pair the phone (USB or WiFi)

Plug the phone in via USB, enable USB debugging in Developer Options, then:

```bash
adroid-pair --list                    # see connected devices
adroid-pair                           # interactive verify
adroid-pair --wireless 192.168.1.42:pairing-port --code 123456   # WiFi pairing (Android 11+)
```

The phone will show an "Allow USB debugging?" prompt with your laptop's
RSA fingerprint. Tap **Allow** (and check "Always allow from this computer"
to skip future prompts).

### Step 2: Start the runtime

```bash
adroid-start --bridge adb --port 7654
```

The runtime prints a **starter agent token** (24h TTL, ACT scope on all 5
tools). Copy it — you'll give it to your AI agent.

### Step 3: Expose to the internet (so the agent can reach it)

If the agent runs on a different machine (e.g. a cloud-hosted LLM):

```bash
# Option A: cloudflared (recommended, free, no signup)
cloudflared tunnel --url http://localhost:7654

# Option B: ngrok
ngrok http 7654
```

You'll get a public URL like `https://random-name.trycloudflare.com`.

### Step 4: Open the dashboard

Open `http://localhost:7654/ui` in your laptop browser. You'll see:
- **Pending Approvals** panel — empty for now, will fill as the agent makes ACT-scope requests
- **Agent Terminal** panel — shows every request, result, error in real time
- **Latest Screenshot** panel — auto-refreshes when the agent captures one

### Step 5: Give the agent URL + token

Tell your AI agent (e.g. paste into chat):

> "Use Adroid runtime at `https://random-name.trycloudflare.com`.
> Here's your token: `<paste token JSON>`.
> Use POST /agent/call/async with `{token, tool, args}`.
> For ACT-scope tools, you'll get back `approval_url` — share it with me
> and I'll approve in my browser."

### Step 6: Watch the magic

When the agent calls `app.launch` (ACT scope):
1. Agent POSTs → gets back `approval_url`
2. Agent shares URL with you in chat
3. You click URL → dashboard scrolls to highlighted pending card
4. You tap **Approve** (or **Deny**)
5. Agent polls → gets result → continues

Every step is in the audit log + live terminal.

### Mode: Mock (no phone needed — for dev / demo)

```bash
adroid-start --bridge mock --port 7654
```

Same flow, but the "phone" is a mock in memory. Useful for testing the
agent API without any hardware.

## Using the agent API

### Async flow (recommended for chat-based agents)

```python
import requests
import time

RUNTIME_URL = "https://your-cloudflare-url.trycloudflare.com"
TOKEN_JSON = {...}  # the starter token from runtime output

# READ scope — completes immediately
res = requests.post(f"{RUNTIME_URL}/agent/call/async", json={
    "token": TOKEN_JSON,
    "tool": "device.list",
    "args": {},
}, timeout=30)
body = res.json()
if body["status"] == "completed":
    devices = body["result"]["devices"]
    print(f"Found {len(devices)} device(s)")

# ACT scope — returns pending_approval + URL to share with the human
res = requests.post(f"{RUNTIME_URL}/agent/call/async", json={
    "token": TOKEN_JSON,
    "tool": "app.launch",
    "args": {
        "device_id": {"kind": "adb", "value": "emulator-5554"},  # or your device serial
        "package_name": "com.example.app",
    },
}, timeout=30)
body = res.json()
if body["status"] == "pending_approval":
    print(f"Hey human, please approve: {body['approval_url']}")
    # Poll until terminal
    while True:
        r = requests.get(f"{RUNTIME_URL}/agent/result/{body['approval_id']}", timeout=10)
        state = r.json()
        if state["state"] in ("completed", "denied", "timeout", "error"):
            break
        time.sleep(2)
    if state["state"] == "completed":
        print(f"Launched! result={state['result']}, approved by {state['approver']}")
    else:
        print(f"Failed: state={state['state']}, error={state.get('error')}")
```

### Sync flow (for non-chat agents that can hold connections)

```python
import requests

RUNTIME_URL = "https://your-cloudflare-url.trycloudflare.com"
TOKEN_JSON = {...}

# Blocks until approval or denial (default timeout 60s, configurable on the server)
res = requests.post(f"{RUNTIME_URL}/agent/call", json={
    "token": TOKEN_JSON,
    "tool": "app.launch",
    "args": {"device_id": {"kind": "adb", "value": "emulator-5554"}, "package_name": "com.example.app"},
}, timeout=120)
body = res.json()
# body["ok"] is True on success, False on denial/error
# body["approval_id"] is set if interactive approval happened
```

### Endpoints summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/agent/call` | Sync agent call (blocks on approval) |
| POST | `/agent/call/async` | Async agent call (returns approval URL for ACT scope) |
| GET | `/agent/result/{approval_id}` | Poll for async result |
| GET | `/api/pending` | List pending approvals (browser dashboard uses this) |
| POST | `/api/approve/{approval_id}` | Approve a pending request (browser-only) |
| POST | `/api/deny/{approval_id}` | Deny a pending request (browser-only) |
| GET | `/api/history` | Past approvals (capped at 100) |
| GET | `/api/terminal` | Agent terminal history |
| GET | `/api/audit` | Audit log tail |
| GET | `/api/tools` | Registered tool specs |
| GET | `/blob/{blob_ref}` | Screenshot metadata |
| GET | `/blob/{blob_ref}/raw` | Screenshot PNG bytes |
| WS | `/ws` | Live terminal + pending updates |
| GET | `/ui` | Browser dashboard |

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
