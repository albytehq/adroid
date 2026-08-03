#!/data/data/com.termux/files/usr/bin/bash
# Adroid phone bootstrap — run inside Termux on the phone.
#
# Sets up:
#   - Python 3 + pip
#   - termux-api (CLI client for Termux:API app)
#   - Adroid runtime + web extras
#   - ngrok (for exposing the runtime to the internet so AI agents can reach it)
#
# After running this script, start the runtime with:
#
#     adroid start --bridge termux --port 7654
#
# Then in another Termux session:
#
#     ngrok http 7654
#
# Open the ngrok URL in your phone browser to see the permission dashboard.
# Give the URL + starter token to your AI agent.

set -e

echo "================================================"
echo "  Adroid bootstrap for Termux"
echo "================================================"
echo

# 1. Update packages
echo "[1/5] Updating packages..."
pkg update -y >/dev/null 2>&1
pkg install -y python git termux-api curl >/dev/null 2>&1
echo "  done."
echo

# 2. Install Adroid from GitHub
echo "[2/5] Installing Adroid from GitHub..."
pip install --upgrade pip >/dev/null 2>&1
pip install "git+https://github.com/albytehq/adroid.git@main#egg=adroid[web,termux]" >/dev/null 2>&1
echo "  done."
echo

# 3. Verify termux-api works
echo "[3/5] Verifying termux-api..."
if command -v termux-screenshot >/dev/null 2>&1; then
    echo "  termux-screenshot: OK"
else
    echo "  WARNING: termux-screenshot not found."
    echo "  Make sure you installed Termux:API from F-Droid (NOT Play Store)."
    echo "  After installing, run: pkg install termux-api"
    exit 1
fi
echo

# 4. Check Termux:API app is installed (the actual Android app, not just the CLI)
echo "[4/5] Verifying Termux:API Android app..."
if pm list packages 2>/dev/null | grep -q com.termux.api; then
    echo "  com.termux.api: installed"
else
    echo "  WARNING: com.termux.api (the Termux:API Android app) not found."
    echo "  The CLI tools will fail without it."
    echo "  Install from: https://f-droid.org/packages/com.termux.api/"
    echo "  (Do NOT use the Play Store version — it is deprecated and broken.)"
fi
echo

# 5. Download ngrok for exposing the runtime to the internet
echo "[5/5] Checking for ngrok..."
if command -v ngrok >/dev/null 2>&1; then
    echo "  ngrok: already installed"
else
    echo "  ngrok not found."
    echo "  To install:"
    echo "    pkg install golang"
    echo "    go install golang.org/x/mobile/cmd/gobind@latest"
    echo "  OR use cloudflared instead:"
    echo "    pkg install cloudflared"
    echo "    cloudflared tunnel --url http://localhost:7654"
fi
echo

echo "================================================"
echo "  Setup complete!"
echo "================================================"
echo
echo "Next steps:"
echo
echo "  1. Start the Adroid runtime:"
echo "       adroid start --bridge termux --port 7654"
echo
echo "  2. In another Termux session, expose it to the internet:"
echo "       cloudflared tunnel --url http://localhost:7654"
echo "     (or: ngrok http 7654)"
echo
echo "  3. Open the dashboard URL in your phone browser:"
echo "       http://localhost:7654/ui"
echo
echo "  4. Copy the starter token from the runtime output."
echo
echo "  5. Give the public URL + starter token to your AI agent."
echo
echo "  6. Watch the dashboard — approve/deny every ACT scope request."
echo
