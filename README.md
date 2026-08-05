# Adroid

> Self-hosted, contract-governed runtime that gives AI agents a stable,
> audit-grade surface to observe, request, and act on Android devices —
> with **one-time pairing** so the device owner approves the AI once,
> then it has full access until they disconnect.

**Status:** v0.3.6 · **License:** Apache-2.0 · **Python:** ≥3.10

Adroid is not a wrapper around ADB. It is a runtime that owns the contract
and treats drivers as replaceable details. The public API survives swaps
between ADB, Termux, UIAutomator, accessibility services, and future
drivers that do not exist yet.

The deal: **AI pairs once → full access until TTL/disconnect**. No
per-action approval friction. Every call is signed-audited and live-streamed
to a browser dashboard so the human always sees what the AI is doing.
Click Disconnect anytime to instantly revoke.

## What's in v0.3.6

- ✅ Typed contract layer (Pydantic v2)
- ✅ Capability-scoped tokens (Ed25519-signed)
- ✅ Permission gate with per-tool rate limiting + revocation check
- ✅ Append-only, hash-chained, Ed25519-signed audit log
- ✅ Abstract `DeviceBridge` Protocol + ADB + Mock + Termux implementations
- ✅ Runtime orchestrator that ties it all together
- ✅ **Session-based pairing** — one-time approval → full access until TTL/disconnect
- ✅ **Bootstrap token auth** — only the AI with the bootstrap token can request pairing
- ✅ **User-set TTL** — human picks session duration at pairing time (1m to 365d)
- ✅ **Live disconnect** — human can revoke any session instantly from dashboard
- ✅ **Live terminal stream** — every AI request/result/error shown in real time
- ✅ **Audit log** — every call recorded with signed hash chain
- ✅ **WebSocket** — live dashboard updates (terminal + pending pairings + active sessions)
- ✅ Optional MCP server layer (`pip install adroid[mcp]`)
- ✅ Up to 20 concurrent AI sessions per runtime

## Architecture

```
                ┌──────────────────────────────────────────┐
                │         AI Agent (HTTP client)           │
                │   any chat agent / script / IDE plugin   │
                └──────────────────┬───────────────────────┘
                                   │
                                   │ 1. POST /agent/session/request
                                   │    { bootstrap_token, agent_id }
                                   │
                                   │ 5. GET /agent/session/{pairing_id}
                                   │    poll until approved
                                   │
                                   │ 6. POST /agent/call
                                   │    { session_token, tool, args }
                                   │    (no per-action approval!)
                                   ▼
                ┌──────────────────────────────────────────┐
                │      LAPTOP (self-hosted server)         │
                │  ┌────────────────────────────────────┐  │
                │  │  Adroid Runtime (Python)           │  │
                │  │  ┌──────────┐  ┌────────────────┐  │  │
                │  │  │ Contract │  │ Audit Log      │  │  │
                │  │  │ (typed)  │  │ (signed chain) │  │  │
                │  │  └──────────┘  └────────────────┘  │  │
                │  │  ┌────────────────────────────────┐ │  │
                │  │  │ SessionManager                 │ │  │
                │  │  │  ├── PairingRequest (pending)  │ │  │
                │  │  │  ├── Session (TTL, revocable)  │ │  │
                │  │  │  └── Bootstrap token (1x use)  │ │  │
                │  │  └────────────────────────────────┘ │  │
                │  │  ┌────────────────────────────────┐ │  │
                │  │  │ AdbBridge → ADB → USB/WiFi     │ │  │
                │  │  └────────────────────────────────┘ │  │
                │  └────────────────────────────────────┘  │
                │                                          │
                │  Browser (your laptop):                  │
                │    /ui  →  approve pairing + TTL         │
                │             + live terminal stream       │
                │             + disconnect button          │
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

### Pairing flow (one-time approval, then full access)

1. **Runtime boots** on laptop → prints a **bootstrap_token** to stderr.
2. **AI agent** POSTs `bootstrap_token` + `agent_id` to `/agent/session/request`.
3. **Server** creates a `PairingRequest`, returns `{pairing_id, pairing_url}`.
4. **AI agent** shares the `pairing_url` with the human in chat.
5. **Human** opens `pairing_url` in browser → dashboard shows the pairing request with a TTL selector.
6. **Human** picks TTL (1m, 5m, 1h, 6h, 24h, 7d, or custom) → taps **Approve & Connect**.
7. **Server** mints a session token with ALL capabilities at ACT scope, TTL-set.
8. **AI agent** polls `/agent/session/{pairing_id}` → gets the session_token.
9. **AI agent** now calls `/agent/call` freely — no per-action approval. Every call is audited + streamed to the dashboard terminal.
10. **Session ends** when:
    - TTL expires (token's `expires_at` fires)
    - Human clicks **Disconnect** in dashboard (token_id added to revoke list)
    - Runtime restarts (in-memory state lost)

**No per-action approval.** One pairing → full access until expiry/disconnect. Audit log records every call; live terminal stream shows what the AI is doing in real time.

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

### Step 1: Pair the phone (USB or wireless — no USB required for Android 11+)

Three ways to connect your phone to the laptop:

#### Option A: Android 11+ wireless debugging (recommended, no USB cable needed)

1. On the phone: **Settings → System → Developer options → Wireless debugging** → toggle ON
2. Tap **Pair device with pairing code** — phone shows `IP:port` and a 6-digit code
3. On the laptop:
   ```bash
   adroid pair wireless --ip 192.168.1.42 --port 4321 --code 123456
   # Then connect (pairing port ≠ connect port — see phone's main wireless debugging screen for IP:port)
   adroid pair connect 192.168.1.42:5555
   ```
4. Tap **Allow** on the phone's debugging prompt (one-time)

#### Option B: USB cable (classic, works on all Android versions)

```bash
adroid pair list                    # see connected devices
adroid pair verify                  # auto-pick first device
```

Plug in USB cable → tap **Allow** on phone's "Allow USB debugging?" prompt.

#### Option C: WiFi ADB via tcpip (Android < 11, or wireless debugging unavailable)

One-time USB cable to enable tcpip mode, then wireless forever after:

```bash
adroid pair tcpip 192.168.1.42      # Android < 11 only
# Follow the prompts:
#   1. Plug in USB cable → press Enter
#   2. adroid runs `adb tcpip 5555` on the phone
#   3. Unplug USB → press Enter
#   4. adroid connects to 192.168.1.42:5555 over WiFi
```

Note: tcpip mode resets when the phone reboots. Re-run this command after a reboot.
For Android 11+, prefer Option A — it survives reboots.

#### Verify the phone is ready (any option)

```bash
adroid pair verify                  # auto-pick first device
adroid pair verify 192.168.1.42:5555  # specific serial
```

### Step 2: Start the runtime

```bash
adroid start --bridge adb --port 7654
```

The runtime prints a **bootstrap token** (30-day TTL, no grants — only
used to request a pairing). Copy it — you'll give it to your AI agent.

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
- **Pairing Requests & Active Sessions** panel — empty for now, will show the AI's pairing request when it arrives
- **Agent Terminal** panel — shows every request, result, error in real time
- Status pill at top: `idle` → `pairing` (yellow) → `connected` (green)

### Step 5: Give the agent URL + bootstrap token

Tell your AI agent (e.g. paste into chat):

> "Use Adroid runtime at `https://random-name.trycloudflare.com`.
> Here's the bootstrap token: `<paste bootstrap token JSON>`.
> POST to `/agent/session/request` with `{bootstrap_token, agent_id}`.
> You'll get back a `pairing_url` — share it with me and I'll approve.
> Then poll `/agent/session/{pairing_id}` to get your session_token.
> After that, call `/agent/call` freely until I disconnect."

### Step 6: Watch the magic

1. Agent POSTs `/agent/session/request` → gets `pairing_url`
2. Agent shares the URL with you in chat
3. You click URL → dashboard shows the pairing request with a TTL picker
4. You pick TTL (1m, 5m, 1h, 6h, 24h, 7d, or custom) → tap **Approve & Connect**
5. Agent polls → gets session_token → starts calling tools freely
6. Every call shows up in the live terminal stream + signed audit log
7. Click **Disconnect** on any session card to instantly revoke

No per-action approval. The AI has full access until TTL expires or you disconnect.

### Mode: Mock (no phone needed — for dev / demo)

```bash
adroid start --bridge mock --port 7654
```

Same flow, but the "phone" is a mock in memory. Useful for testing the
agent API without any hardware.

## Using the agent API

### Step 1: Request pairing (once per session)

```python
import requests
import time

RUNTIME_URL = "https://your-cloudflare-url.trycloudflare.com"
BOOTSTRAP_TOKEN = {...}  # the bootstrap token from runtime stderr output

# Request a pairing — server creates a pending request for the human
res = requests.post(f"{RUNTIME_URL}/agent/session/request", json={
    "bootstrap_token": BOOTSTRAP_TOKEN,
    "agent_id": "my-ai-agent-v1",
}, timeout=30)
body = res.json()
pairing_id = body["pairing_id"]
pairing_url = body["pairing_url"]

# Share pairing_url with the human (chat, email, whatever)
print(f"Hey human, please approve: {pairing_url}")

# Poll until decided
while True:
    r = requests.get(f"{RUNTIME_URL}/agent/session/{pairing_id}", timeout=10)
    state = r.json()
    if state["status"] in ("approved", "denied", "expired"):
        break
    time.sleep(2)

if state["status"] != "approved":
    print(f"Pairing failed: {state['status']}")
    exit(1)

session_token = state["session_token"]
# Save this — use it for every /agent/call until TTL expires
```

### Step 2: Call tools freely (no per-action approval)

```python
# Once paired, the AI has full access until TTL/disconnect
res = requests.post(f"{RUNTIME_URL}/agent/call", json={
    "token": session_token,
    "tool": "device.list",
    "args": {},
})
devices = res.json()["result"]["devices"]

res = requests.post(f"{RUNTIME_URL}/agent/call", json={
    "token": session_token,
    "tool": "app.launch",
    "args": {
        "device_id": devices[0]["id"],  # use the device from list
        "package_name": "com.whatsapp",
    },
})
# No approval needed — already paired!

res = requests.post(f"{RUNTIME_URL}/agent/call", json={
    "token": session_token,
    "tool": "device.screenshot",
    "args": {"device_id": devices[0]["id"]},
})
blob_ref = res.json()["result"]["screenshot"]["blob_ref"]
# Fetch the PNG bytes:
png_bytes = requests.get(f"{RUNTIME_URL}/blob/{blob_ref}/raw").content
```

### Step 3: Handle revocation gracefully

If the human clicks Disconnect (or TTL expires), the next `/agent/call`
returns:

```json
{
  "ok": false,
  "error": {
    "code": "adroid.session.revoked",
    "message": "session has been disconnected by the user"
  }
}
```

The AI should detect this and request a new pairing.

### Endpoints summary

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/agent/session/request` | AI requests pairing (needs bootstrap_token) |
| GET | `/agent/session/{pairing_id}` | AI polls for session_token |
| POST | `/agent/call` | AI executes a tool (needs session_token) |
| GET | `/api/state` | Dashboard: pairings + sessions + terminal |
| GET | `/api/audit` | Audit log tail |
| GET | `/api/tools` | Registered tool specs |
| POST | `/api/pair/{pairing_id}/approve` | Browser: approve with TTL |
| POST | `/api/pair/{pairing_id}/deny` | Browser: deny |
| POST | `/api/session/{session_id}/disconnect` | Browser: revoke session |
| GET | `/blob/{blob_ref}/raw` | Screenshot PNG bytes |
| WS | `/ws` | Live terminal + state updates |
| GET | `/ui` | Browser dashboard |

## Stability promise (v0.3.6)

Once v0.3.6 ships, the following are frozen until v1.0.0:

- **Capability enum values** (`device.list`, `device.observe`, etc.)
- **Scope enum values** (`read`, `act`, `admin`)
- **AuditEvent schema** — every field name and type
- **Audit hash + signature domains** (`adroid.audit.v1`, `adroid.audit.sig.v1`)
- **CapabilityToken canonical serialization** — byte-stable JSON shape
- **ToolSpec names + required args schema** for tools marked `stability="stable"`
- **Agent API endpoints** (`POST /agent/call`, `POST /agent/session/request`, `GET /agent/session/{pairing_id}`, `POST /api/pair/{id}/approve`, `POST /api/pair/{id}/deny`, `POST /api/session/{id}/disconnect`)

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
│   └── cli/             # `adroid` entrypoint (Typer app + subcommands)
├── scripts/
│   ├── install_termux.sh  # Phone bootstrap
│   └── e2e_test.py        # End-to-end integration test
├── tests/               # 314 passing tests across 13 modules
├── examples/            # Runnable end-to-end demo
└── pyproject.toml
```

## Roadmap

| Version  | Months | Theme                | Key deliverables                                |
|----------|--------|----------------------|-------------------------------------------------|
| v0.1.0   | 1–4    | MVP                  | Core runtime, ADB/Termux/Mock bridges, web permission UI, 13 tools (shipped) |
| v0.2.0   | 5–8    | Semantic UI          | ui.dump, ui.find_elements, ui.tap_element, ui.wait_for + 4 more tools (shipped) |
| v0.3.0   | 9–12   | Memory + Self-Healing | ConfidenceTracker, MemoryStore, SelfHealingSelector (shipped) |
| v0.3.5   | 13–14  | Rust extension #1    | uiautomator parser (2.8x faster) (shipped) |
| v0.3.6   | 15     | Rust extension #2    | WorldState diff engine (11x faster hash) (shipped) |
| v0.4.0   | 16–18  | Accessibility Service| Domain plugins, on-device event hook, more tools (planned) |
| v0.5.0   | 19–22  | Hardening            | Persistent ADB connection, remote blob store, OpenAPI freeze |
| v1.0.0   | 23–24  | GA                   | 32 MCP tools, 99.9% SLO, STRIDE-complete         |

## Contributing

v0.3.6 is founder-led. PRs welcome once we hit v0.5.0.

## License

Apache-2.0. See `LICENSE`.
