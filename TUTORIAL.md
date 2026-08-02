# Adroid — Joint Testing Tutorial

> Step-by-step guide for testing Adroid together: founder (you, on laptop + phone) + AI agent (me, remote).

This tutorial walks through the **full end-to-end flow** we'll use to test together:

1. You set up the runtime on your laptop
2. You pair your Android phone (wireless or USB)
3. You expose the runtime to the internet
4. I (the AI) request pairing via the API
5. You approve in your browser
6. I get full access to your phone
7. We test all 5 tools live
8. You disconnect when done

**Time needed:** 20–40 minutes (depending on whether you've used ADB before)

---

## Prerequisites

### On your laptop

- **Python 3.10+** — check with `python3 --version`
- **git** — check with `git --version`
- **adb** (Android Debug Bridge) — install:
  - Debian/Ubuntu: `sudo apt install android-tools-adb`
  - macOS: `brew install android-tools`
  - Windows: download [platform-tools](https://developer.android.com/tools/releases/platform-tools) and add to PATH
- **cloudflared** OR **ngrok** — for exposing the runtime to the internet so I can reach it
  - cloudflared: `brew install cloudflared` (macOS) or [download](https://github.com/cloudflare/cloudflared/releases) (Linux/Windows)
  - ngrok: https://ngrok.com/download

### On your Android phone

- **Android 5.0+** (Lollipop or newer)
- **USB debugging** enabled in Developer Options
  - Settings → About phone → tap "Build number" 7 times to enable Developer Options
  - Settings → System → Developer options → USB debugging → ON
- **Same WiFi network** as your laptop (for wireless mode)

---

## Step 1 — Install Adroid on your laptop

```bash
git clone https://github.com/albytehq/adroid.git
cd adroid
pip install -e ".[web,dev,mcp]"
```

Verify the install worked:

```bash
pytest --version    # should print pytest version
adroid-pair --help  # should print help text
```

If `adroid-pair` is not found, your pip bin directory may not be in PATH. Try:

```bash
python -m adroid.pair --help
```

---

## Step 2 — Pair your phone

Pick ONE of three options. **Option A is recommended** (no USB cable needed, survives reboots).

### Option A: Android 11+ wireless debugging (recommended, no USB)

1. On your phone: **Settings → System → Developer options → Wireless debugging** → toggle ON
2. Tap **"Pair device with pairing code"**
3. The phone shows something like:
   ```
   Wi-Fi pairing code: 123456
   IP address & port: 192.168.1.42:4321
   ```
4. On your laptop:
   ```bash
   adroid-pair --pair 192.168.1.42:4321 --code 123456
   ```
5. After successful pairing, **go back to the main Wireless debugging screen** (not the pairing screen) and look at the IP & port shown at the top — that's the *connect* port (different from the pairing port). Example: `192.168.1.42:5555`
6. Connect:
   ```bash
   adroid-pair --connect 192.168.1.42:5555
   ```
7. Tap **Allow** on the phone's debugging prompt (one-time)

### Option B: USB cable (classic, all Android versions)

1. Plug in the USB cable
2. On your laptop:
   ```bash
   adroid-pair --list    # should show your device
   adroid-pair           # auto-verify the first device
   ```
3. Tap **Allow** on the phone's "Allow USB debugging?" prompt
4. Check "Always allow from this computer" to skip future prompts

### Option C: WiFi ADB via tcpip (Android < 11, or wireless debugging unavailable)

1. Plug in USB cable
2. Run on laptop:
   ```bash
   adroid-pair --wireless-setup 192.168.1.42
   ```
3. Follow the prompts:
   - Press Enter after plugging USB
   - Wait for `adb tcpip 5555` to run
   - Unplug USB
   - Press Enter to connect over WiFi
4. Note: tcpip mode resets when the phone reboots. Re-run this command after a reboot.

### Verify (any option)

```bash
adroid-pair --verify
```

You should see:
```
[ok] device model: Pixel 8 Pro
[ok] Android version: 14
[ok] testing screencap...
[ok] screencap works (1234567 bytes)

============================================================
Device ready for Adroid!
============================================================
```

If verification fails, see [Troubleshooting](#troubleshooting) below.

---

## Step 3 — Start the Adroid runtime

```bash
adroid-start --bridge adb --port 7654
```

You'll see output like:

```
======================================================================
Adroid runtime ready.

  Web UI:          http://localhost:7654/ui
  Agent API:       http://localhost:7654/agent/call
  Pairing API:     http://localhost:7654/agent/session/request
  WebSocket:       ws://localhost:7654/ws
  Audit log:       ./adroid.auditlog
  Bridge:          adb
  Issuer ID:       adroid-abc12345
  Browser session: browser-def67890
  Max sessions:    20

Bootstrap token (give this to your AI agent):

{"token_id":"...","issuer":"adroid-abc12345","subject":"bootstrap","issued_at":"...","expires_at":"...","grants":[],"signature":"..."}

Agent flow:
  1. AI POSTs bootstrap_token + agent_id to /agent/session/request
  2. AI gets back pairing_url → shares it with you in chat
  3. You open the URL → pick TTL → Approve
  4. AI polls /agent/session/{pairing_id} → gets session_token
  5. AI calls /agent/call freely with session_token
  6. Click Disconnect in browser to revoke

To expose to the internet: cloudflared tunnel --url http://localhost:7654
======================================================================
```

**Copy the entire bootstrap token JSON** (the line starting with `{"token_id"`). You'll give it to me.

**Leave this terminal running.** Open a new terminal for the next step.

---

## Step 4 — Expose the runtime to the internet

In a **new terminal window** (keep the runtime running in the first one):

```bash
# Option A: cloudflared (recommended, free, no signup)
cloudflared tunnel --url http://localhost:7654
```

Or:

```bash
# Option B: ngrok (requires signup)
ngrok http 7654
```

You'll get a public URL like:
- cloudflared: `https://random-words-1234.trycloudflare.com`
- ngrok: `https://abc-12-34-56-78.ngrok-free.app`

**Copy this URL.** You'll give it to me.

**Leave this terminal running too.** Open the dashboard in your browser next.

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

Keep this tab open. This is where you'll approve my pairing request and watch my actions.

---

## Step 6 — Give me the URL + bootstrap token

In our chat, send me:

```
Runtime URL: https://your-cloudflare-url.trycloudflare.com
Bootstrap token: <paste the JSON from Step 3>
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

1. Click the URL I sent you (or paste it in your browser)
2. The dashboard will scroll to + highlight my pairing request card
3. The card shows:
   - My agent_id (e.g. `super-z`)
   - When I requested it
   - A **TTL selector**: `1m | 5m | 1h | 6h | 24h | 7d | custom`
4. Pick a TTL. For our first test, **start with 1 hour** (so we have time to test, but it auto-expires if you forget to disconnect)
5. Click **"Approve & Connect"**

The status pill will change from `idle` (gray) → `pairing` (yellow) → `connected` (green).

A new session card appears with:
- My agent_id
- Session ID (first 8 chars)
- Created at / expires at timestamps
- Live countdown to expiry
- **Disconnect** button

---

## Step 9 — I poll and get my session token (my side)

Once you approve, I poll `/agent/session/{pairing_id}` and get my session_token. I now have full access.

I'll tell you in chat: "Pairing approved! Aku udh punya akses. Mulai test ya."

---

## Step 10 — We test all 5 tools together

I'll call each tool and tell you what I'm doing. You watch the **Agent Terminal** panel on your dashboard — every request/result/error streams live.

### Tool 1: `device.list` (READ scope)

I call:
```python
POST /agent/call
{"token": <session_token>, "tool": "device.list", "args": {}}
```

You'll see in terminal:
```
[request] device.list({})
[result]   device.list → ok
```

I'll tell you what device(s) I found (model, Android version, etc.).

### Tool 2: `device.observe` (READ scope)

I call with your device's ID:
```python
POST /agent/call
{"token": <session_token>, "tool": "device.observe",
 "args": {"device_id": {"kind": "adb", "value": "<serial>"}}}
```

I'll report back: model, manufacturer, Android version, SDK level, battery %.

### Tool 3: `device.screenshot` (READ scope)

I call:
```python
POST /agent/call
{"token": <session_token>, "tool": "device.screenshot",
 "args": {"device_id": {"kind": "adb", "value": "<serial>"}}}
```

I get back a `blob_ref` (SHA-256 hash). I then fetch the raw PNG bytes from `/blob/{blob_ref}/raw`. **I can now see what's on your phone screen.** 🔥

You'll see in terminal:
```
[request] device.screenshot({"device_id": ...})
[result]   device.screenshot → ok (blob=abc123…, 1080x2400)
```

### Tool 4: `app.list` (READ scope)

I call:
```python
POST /agent/call
{"token": <session_token>, "tool": "app.list",
 "args": {"device_id": {"kind": "adb", "value": "<serial>"}}}
```

I'll get a list of all third-party apps installed on your phone. I'll report back how many apps, maybe highlight a few.

### Tool 5: `app.launch` (ACT scope)

I call:
```python
POST /agent/call
{"token": <session_token>, "tool": "app.launch",
 "args": {"device_id": ..., "package_name": "com.whatsapp"}}
```

**This is the moment of truth.** The app actually opens on your phone. No per-action approval — I already have full access from the pairing.

You'll see in terminal:
```
[request] app.launch({"device_id": ..., "package_name": "com.whatsapp"})
[result]   app.launch → ok
```

And WhatsApp (or whatever app I picked) opens on your phone screen. You can verify by looking at your phone, or I can take another screenshot to confirm.

### Bonus: rapid-fire calls

To prove there's no per-action blocking, I'll do 5 rapid `device.list` calls in a row. You'll see all 5 stream through the terminal in under 0.1 seconds.

---

## Step 11 — You disconnect me

When we're done testing (or anytime you want to revoke my access):

1. In the dashboard, find my session card (the green-bordered one with the countdown)
2. Click **"Disconnect"**
3. Confirm in the browser dialog

The status pill changes from `connected` (green) → `idle` (gray).

My next `/agent/call` will return:
```json
{"ok": false, "error": {"code": "adroid.session.revoked", "message": "session has been disconnected by the user"}}
```

I'll tell you in chat: "Disconnected. Aku gak bisa akses HP lo lagi."

---

## Step 12 — Cleanup

When we're completely done:

1. Press `Ctrl+C` in the **cloudflared/ngrok terminal** to stop the tunnel
2. Press `Ctrl+C` in the **adroid-start terminal** to stop the runtime
3. (Optional) On your phone: turn off USB debugging or wireless debugging if you don't need it
4. (Optional) Inspect the audit log to see everything that happened:
   ```bash
   cat adroid.auditlog | python -m json.tool
   ```

The audit log is signed and hash-chained — you can verify no entries were tampered with by running:
```bash
python -c "
from pathlib import Path
from adroid.audit import AuditReader
from adroid.permissions.tokens import load_public_key
# You'd need to save the audit public key separately — for now just inspect the JSON
"
```

---

## Troubleshooting

### "adb: command not found"

Install adb:
- Debian/Ubuntu: `sudo apt install android-tools-adb`
- macOS: `brew install android-tools`
- Windows: download platform-tools from developer.android.com

### "no devices/emulators found" or empty device list

- USB mode: try a different USB cable (some are charge-only)
- USB mode: make sure USB debugging is ON in Developer Options
- USB mode: tap "Allow" on the phone's RSA prompt
- Wireless mode: make sure phone and laptop are on the same WiFi
- Wireless mode: make sure wireless debugging is ON, not just USB debugging

### "unauthorized" device state

The phone is showing an "Allow USB debugging?" dialog. Unlock the phone, tap **Allow**, and check "Always allow from this computer".

### "device offline" state

- Reboot the phone
- Try a different USB cable / port
- For wireless: toggle wireless debugging OFF then ON

### Pairing code expired (Android 11+ wireless)

The 6-digit pairing code is only valid for ~30 seconds. Generate a new one and run `adroid-pair --pair` immediately.

### Wrong port used for connect vs pair

Android 11+ wireless debugging has TWO ports:
- **Pairing port** (under "Pair device with pairing code") — used with `--pair`
- **Connect port** (top of main wireless debugging screen) — used with `--connect`

They're different numbers. Don't mix them up.

### `adroid-start` says "port 7654 already in use"

Another process is using that port. Either kill it or use a different port:
```bash
adroid-start --bridge adb --port 7655
```

### cloudflared/ngrok URL doesn't reach the runtime

- Make sure `adroid-start` is running (check the terminal — you should see "Application startup complete")
- Make sure you're tunneling to the right port (7654 by default)
- Try the URL in your own browser first: `https://your-url.trycloudflare.com/api/tools` — should return JSON

### Bootstrap token rejected with 401

You probably copied the token wrong. Re-copy the entire JSON line from the `adroid-start` output — it should start with `{"token_id"` and end with `}`. Make sure no characters got cut off.

### I (the AI) get "adroid.session.revoked" on a call

You clicked Disconnect. If you want me back, request a new pairing — I'll send a new URL, you approve again.

### Screenshot returns 0 bytes or non-PNG

Some devices don't allow `screencap` without root. Try:
```bash
adb exec-out screencap -p > test.png
```
If that fails, the device may need root, or you need to use a different bridge (Termux bridge can take screenshots without root via `termux-screenshot`).

---

## Testing checklist (for our joint session)

Use this to track our progress:

- [ ] You installed Adroid on your laptop
- [ ] You paired your phone (USB / wireless / tcpip)
- [ ] You verified the device with `adroid-pair --verify`
- [ ] You started the runtime with `adroid-start`
- [ ] You exposed it with cloudflared/ngrok
- [ ] You opened the dashboard at `http://localhost:7654/ui`
- [ ] You gave me the URL + bootstrap token
- [ ] I requested pairing
- [ ] You approved with TTL = 1 hour
- [ ] I confirmed I got the session token
- [ ] I called `device.list` — you saw it in the terminal
- [ ] I called `device.observe` — you saw it in the terminal
- [ ] I called `device.screenshot` — you saw it in the terminal
- [ ] I called `app.list` — you saw it in the terminal
- [ ] I called `app.launch` — the app opened on your phone
- [ ] I did 5 rapid calls — you saw them stream fast
- [ ] You clicked Disconnect — I confirmed I got revoked
- [ ] You cleaned up (stopped runtime + tunnel)

---

## What we're verifying

This isn't just a demo — we're verifying the **core promises** of Adroid:

1. **Self-hosted** — runtime runs on your laptop, not in a cloud
2. **Contract-governed** — every call validated by typed contracts (Pydantic)
3. **Audit-grade** — every call signed + hash-chained in the audit log
4. **One-time pairing** — approve once, full access until TTL/disconnect
5. **User-set TTL** — you control how long I have access
6. **Live disconnect** — you can revoke instantly, anytime
7. **Live monitoring** — terminal stream shows everything I do in real time
8. **No per-action friction** — once paired, I can call any tool freely
9. **Wireless-ready** — no USB cable needed for Android 11+
10. **Multiple sessions** — up to 20 AI agents can pair concurrently

If all the checklist items pass, we've proven the v0.1.0 MVP works end-to-end. Time to celebrate 🍻 and plan v0.2.0.

---

## Next steps after this test

Once we've verified v0.1.0 works:

1. **Iterate** — anything that felt clunky? File issues at https://github.com/albytehq/adroid/issues
2. **v0.2.0 planning** — persistent ADB connection, accessibility service bridge (for non-root input injection), OpenAPI freeze
3. **Real-world use cases** — what would you actually use this for? Test those scenarios
4. **Security review** — run the adversarial test suite (Plan D from earlier) to verify the security claims
5. **Multi-device** — v0.1.0 limits to 1 HP per runtime; v0.2.0 will support multiple

Let's ship this. 🔧📱
