# Adroid — Joint Testing Tutorial

> **Wireless mode (Android 11+) — no USB cable needed.**
>
> Step-by-step guide for testing Adroid together: founder (you, on laptop + phone) + AI agent (me, remote).

**Time needed:** 20–40 minutes · **Tools tested:** all 13 v0.1.0 MCP tools · **Prerequisite:** Android 11 or newer

---

## Overview — what we're building

```
┌─────────────────────────────────────────────────────────────┐
│  AI Agent (me, anywhere on the internet)                    │
│  HTTP client → POST /agent/call with session token          │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTPS (via cloudflared/ngrok)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  YOUR LAPTOP (self-hosted server)                           │
│  ┌──────────────────────────────────────────────────────┐  │
│  │ Adroid Runtime (Python)                              │  │
│  │  • 13 MCP tools (device.list, input.tap_text, etc.)  │  │
│  │  • Session-based pairing (one-time approval)         │  │
│  │  • Allowlisted shell.exec (no rm -rf, no compound)   │  │
│  │  • Signed audit log (hash-chained, tamper-evident)   │  │
│  │  • Live dashboard (terminal stream + sessions)       │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                             │
│  Browser (your laptop): http://localhost:7654/ui            │
│    → approve pairing + watch live terminal + disconnect     │
└──────────────────────────┬──────────────────────────────────┘
                           │ ADB over WiFi (same network)
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  YOUR ANDROID PHONE (Android 11+)                           │
│  • Wireless debugging ON (no app install needed)            │
│  • No USB cable needed                                       │
│  • Just tap "Allow" on the pairing prompt (1x)              │
└─────────────────────────────────────────────────────────────┘
```

**The deal:** AI pairs once → full access to all 13 tools until TTL/disconnect. No per-action approval. Every call is signed-audited and live-streamed to your browser dashboard.

---

## Prerequisites

### On your laptop

| Requirement | Check command | Install if missing |
|-------------|---------------|-------------------|
| Python 3.10+ | `python3 --version` | https://python.org |
| git | `git --version` | `sudo apt install git` |
| adb | `adb version` | `sudo apt install adb` |
| cloudflared | `cloudflared --version` | See Step 4 below |

### On your Android phone

- **Android 11 or newer** (check: Settings → About phone → Android version)
- **Developer Options enabled** (Settings → About phone → tap "Build number" 7 times)
- **Same WiFi network** as your laptop

---

## Step 1 — Install Adroid on your laptop

### 1.1 Clone the repository

```bash
cd ~
git clone https://github.com/albytehq/adroid.git
cd adroid
```

### 1.2 Create a virtual environment

**Linux (Ubuntu/Debian):**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e ".[web,dev,mcp]"
```

> ⚠️ **Important:** You must run `source .venv/bin/activate` every time you open a new terminal. You'll know it's active when the prompt shows `(.venv)` at the start.

**macOS / Windows:**

```bash
pip install -e ".[web,dev,mcp]"
```

### 1.3 Verify the install

```bash
adroid doctor
```

You should see all green checkmarks:

```
╭─ Adroid Doctor ──────────────────────────────────╮
│ Running environment diagnostics…                 │
╰──────────────────────────────────────────────────╯

✓ Python 3.12.13
✓ adb binary: /usr/bin/adb
✓ extras [web]: installed
✓ extras [mcp]: installed
✓ Network: can reach internet

╭─ Diagnostics complete ───────────────────────────╮
│ If everything above is green, you're ready to:   │
│   adroid pair list   — see connected devices     │
│   adroid start       — boot the runtime          │
╰──────────────────────────────────────────────────╯
```

If anything is yellow or red, fix it before continuing (see [Troubleshooting](#troubleshooting)).

---

## Step 2 — Pair your phone (wireless, no USB)

### 2.1 Enable Wireless Debugging on your phone

1. **Settings → System → Developer options**
2. Scroll down to **Wireless debugging** → toggle **ON**
3. Tap **Wireless debugging** (the text, not the toggle) to open its settings screen
4. Tap **Pair device with pairing code**

Your phone now displays:

```
┌─────────────────────────────────────┐
│  Pairing code:  123456              │
│                                     │
│  IP address & port:                 │
│  192.168.1.42:4321                  │
└─────────────────────────────────────┘
```

**Write down both:** the IP:port (`192.168.1.42:4321`) and the 6-digit code (`123456`).

> ⚠️ The pairing code expires in ~30 seconds. Have your laptop ready.

### 2.2 Pair from your laptop

In your laptop terminal (with `.venv` activated):

```bash
adroid pair wireless --ip 192.168.1.42 --port 4321 --code 123456
```

**Replace** `192.168.1.42`, `4321`, and `123456` with the actual values from your phone.

You should see:

```
╭─ Wireless Pairing (Android 11+) ─────────────────╮
│ Target: 192.168.1.42:4321                        │
╰──────────────────────────────────────────────────╯

✓ Pairing successful!

ℹ Now CONNECT using the connect port (different from pairing port).
ℹ On the phone's main Wireless Debugging screen, look at 'IP & port' at the top.
ℹ Then run:
  adroid pair connect --ip 192.168.1.42 --port <CONNECT_PORT>
```

### 2.3 Connect to the paired device

**Go back to the main Wireless Debugging screen** on your phone (not the pairing screen). At the top, you'll see:

```
IP address & port
192.168.1.42:5555
```

**This is a different port** from the pairing port. Write it down.

On your laptop:

```bash
adroid pair connect --ip 192.168.1.42 --port 5555
```

**Replace** `5555` with the actual connect port from your phone.

You should see:

```
╭─ Wireless Connect ───────────────────────────────╮
│ Target: 192.168.1.42:5555                        │
╰──────────────────────────────────────────────────╯

✓ Connected to 192.168.1.42:5555

╭─ Devices After Connect ──────────────────────────╮
│        Serial              State     Type      Model   │
│  WIFI  192.168.1.42:5555  device    wireless  Pixel 8 │
╰────────────────────────────────────────────────────────╯
```

### 2.4 Tap "Allow" on your phone

Your phone shows a dialog: **"Allow USB debugging?"** with your laptop's RSA fingerprint.

- Tap **Allow**
- Check **"Always allow from this computer"** to skip future prompts

### 2.5 Verify the device is ready

```bash
adroid pair verify
```

You should see:

```
╭─ Device Verification ────────────────────────────╮
│ Target: auto-pick first ready device             │
╰──────────────────────────────────────────────────╯

✓ Android Debug Bridge version 1.0.41
✓ Auto-picked: 192.168.1.42:5555 (Pixel 8, wireless)
✓ Device model: Pixel 8
✓ Android version: 14
✓ SDK level: 34
ℹ Testing screencap...
✓ screencap works (1,234,567 bytes)

╭─ Device ready for Adroid! ───────────────────────╮
│ Start the runtime with:                          │
│   adroid start --bridge adb                      │
│                                                  │
│ Device ID for agent calls:                       │
│   {"kind": "adb", "value": "192.168.1.42:5555"} │
│                                                  │
│ Wireless connection active. Phone can be anywhere│
│ on this WiFi.                                    │
╰──────────────────────────────────────────────────╯
```

**Write down the Device ID** — I'll need it when calling tools:
```json
{"kind": "adb", "value": "192.168.1.42:5555"}
```

---

## Step 3 — Start the Adroid runtime

```bash
adroid start --bridge adb --port 7654
```

You'll see a beautiful banner with three panels:

### Panel 1: Runtime Ready

```
╭─ Adroid Runtime Ready ───────────────────────────╮
│ Web UI          http://localhost:7654/ui         │
│ Agent API       http://localhost:7654/agent/call │
│ Pairing API     http://localhost:7654/agent/session/request │
│ WebSocket       ws://localhost:7654/ws           │
│ Audit log       adroid.auditlog                  │
│ Bridge          adb                              │
│ Issuer ID       adroid-abc12345                  │
│ Browser session browser-def67890                │
│ Max sessions    20                               │
╰──────────────────────────────────────────────────╯
```

### Panel 2: Bootstrap Token

```
╭─ Bootstrap token ────────────────────────────────╮
│ {"token_id":"...","issuer":"adroid-abc12345",    │
│  "subject":"bootstrap","grants":[],              │
│  "signature":"..."}                              │
╰──────────────────────────────────────────────────╯
```

**Copy the entire bootstrap token JSON.** You'll give it to me.

### Panel 3: Agent Flow

```
╭─ Agent flow ─────────────────────────────────────╮
│ 1. AI POSTs bootstrap_token + agent_id to        │
│    /agent/session/request                        │
│ 2. AI gets back pairing_url → shares it with you │
│ 3. You open the URL → pick TTL → Approve         │
│ 4. AI polls /agent/session/{id} → gets token     │
│ 5. AI calls /agent/call freely with token        │
│ 6. Click Disconnect in browser to revoke         │
│                                                  │
│ To expose to the internet:                       │
│   cloudflared tunnel --url http://localhost:7654 │
╰──────────────────────────────────────────────────╯
```

**Leave this terminal running.** Open a new terminal for the next step.

---

## Step 4 — Expose the runtime to the internet

In a **new terminal window** (keep the runtime running in the first one):

### 4.1 Install cloudflared (if not already installed)

**Linux (amd64):**

```bash
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin/
```

**macOS:**

```bash
brew install cloudflared
```

### 4.2 Start the tunnel

```bash
cloudflared tunnel --url http://localhost:7654
```

You'll see output like:

```
+--------------------------------------------------------------------------------------------+
|  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
|  https://random-words-1234.trycloudflare.com                                               |
+--------------------------------------------------------------------------------------------+
```

**Copy the URL** (`https://random-words-1234.trycloudflare.com`). You'll give it to me.

**Leave this terminal running too.**

> 💡 **Alternative:** If you prefer ngrok, run `ngrok http 7654` instead.

---

## Step 5 — Open the dashboard

In your laptop's browser, open:

```
http://localhost:7654/ui
```

You'll see the Adroid pairing dashboard:

- **Status pill** at top right: `idle` (gray)
- **Left panel**: "Waiting for AI agent to request pairing…"
- **Right panel**: "Agent Terminal" — will fill with my activity in real time

**Keep this tab open.** This is where you'll approve my pairing request and watch my actions.

---

## Step 6 — Give me the URL + bootstrap token

In our chat, send me:

```
Runtime URL: https://random-words-1234.trycloudflare.com
Bootstrap token: {"token_id":"...","issuer":"...","subject":"bootstrap",...}
Device ID: {"kind":"adb","value":"192.168.1.42:5555"}
```

That's it. I'll take it from here.

---

## Step 7 — I request pairing (my side)

Once you give me the URL + bootstrap token, I will:

1. POST to `/agent/session/request` with the bootstrap token and my agent_id
2. Get back a `pairing_url` like `https://your-url.trycloudflare.com/ui#pairing-abc123`
3. **Share that URL with you in chat**

You'll see something like:

> "Bro, aku minta pairing nih. Buka URL ini buat approve:
> https://your-url.trycloudflare.com/ui#pairing-abc123"

---

## Step 8 — You approve the pairing

1. **Click the URL** I sent you (or paste it in your browser)
2. The dashboard will **scroll to + highlight** my pairing request card
3. The card shows:
   - My agent_id (e.g. `super-z`)
   - When I requested it
   - A **TTL selector**: `1m | 5m | 1h | 6h | 24h | 7d | custom`
4. **Pick a TTL.** For our first test, use **1 hour** — enough time to test everything, but auto-expires if you forget to disconnect
5. Click **"Approve & Connect"**

The status pill changes: `idle` (gray) → `pairing` (yellow) → `connected` (green).

A new **session card** appears with:
- My agent_id
- Session ID (first 8 chars)
- Created at / expires at timestamps
- Live countdown to expiry
- **Disconnect** button

---

## Step 9 — I poll and get my session token (my side)

Once you approve, I poll `/agent/session/{pairing_id}` and get my session_token. I now have full access to all 13 tools.

I'll tell you in chat: "Pairing approved! Aku udh punya akses. Mulai test ya."

---

## Step 10 — We test all 13 tools together

I'll call each tool and tell you what I'm doing. You watch the **Agent Terminal** panel on your dashboard — every request/result/error streams live.

### Tool 1: `device.list` — List devices

```python
POST /agent/call
{"token": <session_token>, "tool": "device.list", "args": {}}
```

**What it does:** Lists all devices visible to the bridge.

**You'll see in terminal:**
```
[request] device.list({})
[result]   device.list → ok
```

**I'll report back:** device model, Android version, state.

---

### Tool 2: `device.observe` — Get device info

```python
POST /agent/call
{"token": <session_token>, "tool": "device.observe",
 "args": {"device_id": {"kind": "adb", "value": "192.168.1.42:5555"}}}
```

**What it does:** Returns a fresh snapshot of one device's state + properties.

**I'll report back:** model, manufacturer, Android version, SDK level, battery %.

---

### Tool 3: `device.screenshot` — Capture screenshot

```python
POST /agent/call
{"token": <session_token>, "tool": "device.screenshot",
 "args": {"device_id": {"kind": "adb", "value": "192.168.1.42:5555"}}}
```

**What it does:** Captures a PNG screenshot and stores it content-addressed (sha256).

**I'll report back:** blob_ref, dimensions (width × height), and I'll fetch the raw PNG bytes from `/blob/{blob_ref}/raw`. **I can now see what's on your phone screen.** 🔥

**You'll see in terminal:**
```
[request] device.screenshot({"device_id": ...})
[result]   device.screenshot → ok (blob=abc123…, 1080x2400)
```

---

### Tool 4: `app.list` — List installed apps

```python
POST /agent/call
{"token": <session_token>, "tool": "app.list",
 "args": {"device_id": {"kind": "adb", "value": "192.168.1.42:5555"}}}
```

**What it does:** Lists all installed packages on the device.

**I'll report back:** how many apps, maybe highlight a few interesting ones.

---

### Tool 5: `app.launch` — Launch an app

```python
POST /agent/call
{"token": <session_token>, "tool": "app.launch",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "package_name": "com.whatsapp"
 }}
```

**What it does:** Opens an app on your phone. **The app actually appears on your screen.**

**You'll see in terminal:**
```
[request] app.launch({"device_id": ..., "package_name": "com.whatsapp"})
[result]   app.launch → ok
```

And WhatsApp (or whatever app I picked) opens on your phone.

---

### Tool 6: `input.tap` — Tap at coordinates

```python
POST /agent/call
{"token": <session_token>, "tool": "input.tap",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "x": 540, "y": 1200
 }}
```

**What it does:** Injects a tap event at pixel coordinates (540, 1200).

**I'll report back:** `tapped: true, x: 540, y: 1200`

---

### Tool 7: `input.tap_text` — Tap by visible text (semantic) ⭐

```python
POST /agent/call
{"token": <session_token>, "tool": "input.tap_text",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "text": "Sign in",
   "match": "first",
   "scroll_to_visible": true
 }}
```

**What it does:**
1. Dumps the UI hierarchy via `uiautomator dump`
2. Parses the XML, finds elements whose `text` or `content-desc` matches "Sign in"
3. If not found and `scroll_to_visible=true`, swipes down and retries (up to 5 times)
4. Taps the center of the matched element's bounding box

**I'll report back:**
```json
{
  "matched_elements": 1,
  "tapped_element": {"text": "Sign in", "bounds": {"x": 120, "y": 880, "w": 240, "h": 64}},
  "scrolled": false,
  "duration_ms": 312
}
```

This is the **killer tool** — semantic UI automation without knowing exact coordinates.

---

### Tool 8: `input.swipe` — Swipe gesture

```python
POST /agent/call
{"token": <session_token>, "tool": "input.swipe",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "x1": 540, "y1": 1800,
   "x2": 540, "y2": 600,
   "duration_ms": 500
 }}
```

**What it does:** Swipes from (540, 1800) to (540, 600) over 500ms — a scroll-up gesture.

---

### Tool 9: `input.text` — Type text

```python
POST /agent/call
{"token": <session_token>, "tool": "input.text",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "text": "hello world"
 }}
```

**What it does:** Types "hello world" into the currently focused field. Unicode-safe.

---

### Tool 10: `input.key` — Press a key

```python
POST /agent/call
{"token": <session_token>, "tool": "input.key",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "keycode": "KEYCODE_HOME"
 }}
```

**What it does:** Presses an Android keycode. Common ones:
- `KEYCODE_HOME` — go to home screen
- `KEYCODE_BACK` — back button
- `KEYCODE_ENTER` — enter key
- `KEYCODE_VOLUME_UP` / `KEYCODE_VOLUME_DOWN`

---

### Tool 11: `shell.exec` — Allowlisted shell command ⭐

```python
POST /agent/call
{"token": <session_token>, "tool": "shell.exec",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "command": "pm list packages -3"
 }}
```

**What it does:** Executes an allowlisted shell command on the device.

**Allowlisted commands include:**
- `pm list packages` (and variants with flags)
- `getprop` (device properties)
- `dumpsys battery` / `dumpsys window` / `dumpsys notification`
- `logcat -d -t N` (read logs)
- `df`, `ls`, `stat`, `file` (filesystem inspection)
- `ip addr`, `ip route`, `ifconfig`, `netstat` (network)
- `settings get`, `uptime`, `mount`, `free`, `date`

**Forbidden by default:**
- Shell metacharacters: `; | & > $() ${ && || backtick`
- Compound commands: `pm list packages | grep foo` → **REJECTED**
- Dangerous commands: `rm -rf /` → **REJECTED** (doesn't match any pattern)

**I'll report back:**
```json
{
  "stdout": "package:com.example.app1\npackage:com.example.app2\n",
  "stderr": "",
  "returncode": 0,
  "duration_ms": 45
}
```

If I try a forbidden command, I'll get:
```json
{
  "ok": false,
  "error": {
    "code": "adroid.shell.not_allowed",
    "message": "shell command rejected by allowlist: forbidden shell metacharacter: '|'",
    "details": {"command": "pm list packages | grep foo", "reason": "..."}
  }
}
```

---

### Tool 12: `logs.read` — Read logcat

```python
POST /agent/call
{"token": <session_token>, "tool": "logs.read",
 "args": {
   "device_id": {"kind": "adb", "value": "192.168.1.42:5555"},
   "lines": 100,
   "filter": "ActivityManager:I"
 }}
```

**What it does:** Reads the last N logcat entries. Optional filter uses logcat tag syntax.

**I'll report back:**
```json
{
  "lines": ["08-02 12:34:56.123  1234 5678 I ActivityManager: ...", ...],
  "truncated": false,
  "total_bytes": 4567
}
```

---

### Tool 13: `permission.request` — Request additional scope

```python
POST /agent/call
{"token": <session_token>, "tool": "permission.request",
 "args": {"scope": "shell_exec", "reason": "Need to inspect device logs"}}
```

**What it does:** Meta-tool to request an additional permission scope.

**v0.1.0 behavior:** Since session pairing already grants ALL scopes, this always returns `status: "granted"`. In v0.2.0+, this will trigger an actual per-scope approval flow.

**I'll report back:**
```json
{
  "request_id": "abc-123",
  "status": "granted",
  "scope": "shell_exec",
  "note": "v0.1.0: session pairing grants all scopes. v0.2.0+ will require explicit per-scope approval."
}
```

---

### Bonus: Rapid-fire calls

To prove there's no per-action blocking, I'll do 5 rapid `device.list` calls in a row. You'll see all 5 stream through the terminal in under 0.1 seconds.

---

## Step 11 — You disconnect me

When we're done testing (or anytime you want to revoke my access):

1. In the dashboard, find my **session card** (the green-bordered one with the countdown)
2. Click **"Disconnect"**
3. Confirm in the browser dialog

The status pill changes: `connected` (green) → `idle` (gray).

My next `/agent/call` will return:
```json
{
  "ok": false,
  "error": {
    "code": "adroid.session.revoked",
    "message": "session has been disconnected by the user"
  }
}
```

I'll tell you in chat: "Disconnected. Aku gak bisa akses HP lo lagi."

---

## Step 12 — Inspect the audit log

After we're done, check the audit log to see every call I made:

```bash
adroid audit show --limit 50
```

Output:

```
╭─ Audit Log — Last 50 Events ─────────────────────╮
│ File: adroid.auditlog                            │
╰──────────────────────────────────────────────────╯

#   Time              Outcome   Method                    Capability          Subject
0   2026-08-02 13:35  success   runtime.boot              —                   —
1   2026-08-02 13:36  success   runtime.device_list       device.list         session:super-z
2   2026-08-02 13:36  success   runtime.device_observe    device.observe      session:super-z
3   2026-08-02 13:36  success   runtime.device_screenshot device.screenshot   session:super-z
4   2026-08-02 13:37  success   runtime.app_list          app.list            session:super-z
5   2026-08-02 13:37  success   runtime.app_launch        app.launch          session:super-z
6   2026-08-02 13:37  success   runtime.input_tap_text    input.tap_text      session:super-z
7   2026-08-02 13:37  denied    runtime.shell_exec        shell.exec          session:super-z
8   2026-08-02 13:38  success   runtime.shell_exec        shell.exec          session:super-z
9   2026-08-02 13:38  success   runtime.logs_read         logs.read           session:super-z
```

Verify the hash chain + signatures are intact:

```bash
adroid audit verify
```

Output:

```
╭─ Audit Log Verification ─────────────────────────╮
│ File: adroid.auditlog                            │
╰──────────────────────────────────────────────────╯

✓ All 10 events verified successfully.
```

---

## Step 13 — Cleanup

When we're completely done:

1. Press `Ctrl+C` in the **cloudflared terminal** to stop the tunnel
2. Press `Ctrl+C` in the **adroid start terminal** to stop the runtime
3. (Optional) On your phone: turn off Wireless debugging if you don't need it

---

## CLI command reference

```bash
# Diagnostics
adroid doctor                    # check environment + deps
adroid version                   # show version + environment info

# Device pairing (wireless mode)
adroid pair list                 # list connected devices
adroid pair wireless --ip IP --port PORT --code CODE   # Android 11+ pairing
adroid pair connect --ip IP --port PORT                # connect after pairing
adroid pair verify               # verify device is ready
adroid pair verify --serial 192.168.1.42:5555          # specific device

# Runtime
adroid start --bridge adb        # boot runtime with ADB bridge
adroid start --bridge mock       # boot runtime with mock device (no phone)
adroid start --bridge adb --port 8080  # custom port

# Audit log
adroid audit show --limit 50     # show recent events
adroid audit show --json         # output as JSON
adroid audit verify              # verify hash chain + signatures

# Tokens (advanced)
adroid token issue --subject X --grant cap:scope
adroid token inspect <token_json>

# MCP server (optional, for MCP-compatible AI clients)
adroid mcp serve --audit-log /tmp/adroid.auditlog --mock
```

Run `adroid <command> --help` for detailed options on any command.

---

## Troubleshooting

### "adb: command not found"

```bash
sudo apt install adb          # Linux
brew install android-tools    # macOS
```

### "Wireless debugging" not in Developer Options

Your Android version is too old. Wireless debugging requires **Android 11 or newer**. Check: Settings → About phone → Android version.

If you're on Android 10 or older, you'll need to use USB cable mode instead (not covered in this tutorial).

### Pairing code expired

The 6-digit pairing code is only valid for ~30 seconds. Generate a new one:
1. On your phone: tap **Cancel** on the pairing screen
2. Tap **Pair device with pairing code** again
3. Run `adroid pair wireless` immediately with the new code

### "Pairing failed" error

**Causes:**
1. **Phone and laptop not on the same WiFi** → connect both to the same network
2. **Wrong IP or port** → double-check the numbers on your phone's pairing screen
3. **Firewall blocking** → temporarily disable firewall to test
4. **Pairing code already used** → generate a new one (each code is single-use)

### "Connect failed" error

**You used the pairing port instead of the connect port.** They're different:
- **Pairing port** (under "Pair device with pairing code") → used with `adroid pair wireless`
- **Connect port** (top of main Wireless Debugging screen) → used with `adroid pair connect`

Go back to the main Wireless Debugging screen and use the port shown at the top.

### "unauthorized" device state

The phone is showing an "Allow USB debugging?" dialog. Unlock the phone, tap **Allow**, and check **"Always allow from this computer"**.

### "device offline" state

- Toggle Wireless debugging OFF then ON on your phone
- Re-run `adroid pair connect`
- If still offline, reboot the phone and re-pair

### `adroid start` says "port 7654 already in use"

Another process is using that port. Use a different port:
```bash
adroid start --bridge adb --port 7655
```

### cloudflared URL doesn't reach the runtime

- Make sure `adroid start` is running (check the terminal — you should see "Application startup complete")
- Make sure you're tunneling to the right port (7654 by default)
- Test the URL in your own browser: `https://your-url.trycloudflare.com/api/tools` — should return JSON with 13 tools

### Bootstrap token rejected with 401

You probably copied the token wrong. Re-copy the entire JSON line from the `adroid start` output — it should start with `{"token_id"` and end with `}`. Make sure no characters got cut off.

### I (the AI) get "adroid.session.revoked" on a call

You clicked Disconnect. If you want me back, I'll request a new pairing — I'll send a new URL, you approve again.

### Screenshot returns 0 bytes or non-PNG

Some devices don't allow `screencap` without root. Test manually:
```bash
adb -s 192.168.1.42:5555 exec-out screencap -p > test.png
```
If that fails, the device may need root.

### `tap_text` says "no element found"

The text you specified doesn't exist on the current screen. Try:
1. Take a screenshot first to see what's on screen
2. Make sure the text matches exactly (case-sensitive)
3. Set `scroll_to_visible: true` so it scrolls to find the element

### `shell.exec` says "not_allowed"

The command didn't match the allowlist. Either:
1. Use a command that's on the allowlist (see Tool 11 above)
2. Don't use shell metacharacters (`; | & >` etc.) — compound commands are rejected

### `pip install` fails with "Connection broken: IncompleteRead"

Your internet is unstable. Retry the install. If it keeps failing:
- Try a different network (mobile hotspot)
- Use a pip mirror: `pip install -e ".[web,dev,mcp]" -i https://pypi.tuna.tsinghua.edu.cn/simple`

### "externally-managed-environment" error

You're on Ubuntu 24.04+ which uses PEP 668. Use a venv:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[web,dev,mcp]"
```

Remember to `source .venv/bin/activate` every time you open a new terminal.

---

## Testing checklist

Use this to track our progress:

### Setup
- [ ] You ran `adroid doctor` — all green
- [ ] You enabled Wireless debugging on your phone
- [ ] You paired with `adroid pair wireless --ip ... --port ... --code ...`
- [ ] You connected with `adroid pair connect --ip ... --port ...`
- [ ] You tapped "Allow" on the phone's debugging prompt
- [ ] You verified with `adroid pair verify` — all green
- [ ] You started the runtime with `adroid start --bridge adb`
- [ ] You installed cloudflared and started the tunnel
- [ ] You opened the dashboard at `http://localhost:7654/ui`
- [ ] You gave me the URL + bootstrap token + device ID

### Pairing
- [ ] I requested pairing via `/agent/session/request`
- [ ] You opened the pairing URL in your browser
- [ ] You picked TTL = 1 hour
- [ ] You clicked "Approve & Connect"
- [ ] I confirmed I got the session token

### Tools (all 13)
- [ ] `device.list` — I listed your devices
- [ ] `device.observe` — I read device info (model, Android version, battery)
- [ ] `device.screenshot` — I captured a screenshot and saw your screen
- [ ] `app.list` — I listed installed apps
- [ ] `app.launch` — I launched an app on your phone
- [ ] `input.tap` — I tapped at coordinates
- [ ] `input.tap_text` — I tapped an element by visible text (semantic)
- [ ] `input.swipe` — I swiped (scroll gesture)
- [ ] `input.text` — I typed text into a field
- [ ] `input.key` — I pressed a keycode (HOME/BACK/etc.)
- [ ] `shell.exec` — I ran an allowlisted shell command
- [ ] `shell.exec` (denied) — I tried a forbidden command and it was rejected
- [ ] `logs.read` — I read logcat entries
- [ ] `permission.request` — I requested a permission scope

### Bonus
- [ ] I did 5 rapid `device.list` calls — all streamed in <0.1s
- [ ] I tried `rm -rf /` via shell.exec — rejected by allowlist
- [ ] I tried a compound command (`| grep`) — rejected by allowlist

### Teardown
- [ ] You clicked Disconnect — I confirmed I got revoked
- [ ] You ran `adroid audit show` — saw every call
- [ ] You ran `adroid audit verify` — hash chain intact
- [ ] You stopped cloudflared (Ctrl+C)
- [ ] You stopped the runtime (Ctrl+C)

---

## What we're verifying

This isn't just a demo — we're verifying the **core promises** of Adroid v0.1.0:

1. **Self-hosted** — runtime runs on your laptop, not in a cloud
2. **Contract-governed** — every call validated by typed contracts (Pydantic)
3. **Audit-grade** — every call signed + hash-chained in the audit log
4. **One-time pairing** — approve once, full access until TTL/disconnect
5. **User-set TTL** — you control how long I have access
6. **Live disconnect** — you can revoke instantly, anytime
7. **Live monitoring** — terminal stream shows everything I do in real time
8. **No per-action friction** — once paired, I can call any tool freely
9. **Wireless-ready** — no USB cable needed for Android 11+
10. **Shell allowlist** — `rm -rf /` and compound commands are rejected
11. **13 MCP tools** — full v0.1.0 surface per the spec
12. **Multiple sessions** — up to 20 AI agents can pair concurrently
13. **Docker-ready** — Dockerfile + docker-compose.yml for production deploy

If all the checklist items pass, we've proven the v0.1.0 MVP works end-to-end. Time to celebrate 🍻 and plan v0.2.0.

---

Let's ship this. 🔧📱
