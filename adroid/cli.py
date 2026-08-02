"""``adroid-start`` CLI — boot the runtime + web server + bridge.

Architecture: laptop-hosted runtime, ADB bridge to a USB-connected phone,
session-based pairing (one-time approval → full access until TTL/disconnect).

Usage::

    # Mock bridge (no phone needed — for dev / demo)
    adroid-start --bridge mock --port 7654

    # ADB bridge (primary mode — laptop controlling a USB-connected phone)
    adroid-start --bridge adb --port 7654

    # Termux bridge (runtime runs on the phone itself via Termux)
    adroid-start --bridge termux --port 7654

After startup:
    1. Open http://localhost:7654/ui in your browser.
    2. The runtime prints a bootstrap_token — give it to your AI agent.
    3. AI calls POST /agent/session/request with the bootstrap_token.
    4. Browser shows the pairing request → pick TTL → Approve.
    5. AI gets a session_token, can now call any tool freely.
    6. Click Disconnect in browser to revoke anytime.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path

from adroid.bridge.adb import AdbBridge, LocalBlobStore
from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.types import Capability, Scope
from adroid.permissions.sessions import SessionManager
from adroid.permissions.tokens import TokenIssuer
from adroid.runtime.core import AdroidRuntime


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="adroid-start",
        description="Start the Adroid runtime + web pairing UI.",
    )
    parser.add_argument(
        "--bridge",
        choices=["mock", "termux", "adb"],
        default="mock",
        help="Device bridge to use. Default: mock (no real device).",
    )
    parser.add_argument("--port", type=int, default=7654, help="HTTP port. Default 7654.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind address. Default 0.0.0.0.")
    parser.add_argument("--audit-log", default="./adroid.auditlog", help="Audit log path.")
    parser.add_argument("--blob-store-dir", default="./adroid_blobs", help="Blob store dir.")
    parser.add_argument("--adb-path", default=None, help="Path to adb binary (ADB bridge).")
    parser.add_argument("--issuer-id", default=None, help="Runtime issuer ID. Default random.")
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=20,
        help="Max concurrent AI sessions. Default 20.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ------------------------------------------------------------------
    # Bridge + blob store
    # ------------------------------------------------------------------

    if args.bridge == "mock":
        blob_store = InMemoryBlobStore()
        bridge = MockBridge(blob_store=blob_store)
        from adroid.contract.types import DeviceId, DeviceInfo, DeviceState
        bridge.add_device(
            DeviceInfo(
                id=DeviceId(kind="adb", value="mock-emulator-5554"),
                state=DeviceState.ONLINE,
                model="MockPhone",
                manufacturer="Adroid",
                android_version="14",
                sdk_level=34,
                battery_level=87.5,
            )
        )
    elif args.bridge == "termux":
        from adroid.bridge.termux import TermuxBridge
        blob_store = LocalBlobStore(args.blob_store_dir)
        bridge = TermuxBridge(blob_store=blob_store)
        if not TermuxBridge.is_termux():
            sys.stderr.write(
                "WARNING: --bridge termux selected but we don't appear to be inside Termux.\n"
            )
    elif args.bridge == "adb":
        blob_store = LocalBlobStore(args.blob_store_dir)
        bridge = AdbBridge(blob_store=blob_store, adb_path=args.adb_path)
    else:
        sys.stderr.write(f"unknown bridge: {args.bridge}\n")
        sys.exit(2)

    # ------------------------------------------------------------------
    # Runtime
    # ------------------------------------------------------------------

    runtime = AdroidRuntime(
        issuer_id=args.issuer_id,
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path(args.audit_log),
    )

    # ------------------------------------------------------------------
    # Session manager + bootstrap token
    # ------------------------------------------------------------------
    # The bootstrap token is what the AI uses to call /agent/session/request.
    # It is long-lived (30 days), only allows the pairing endpoint (enforced
    # by the endpoint, not the token itself). It does NOT grant any tool
    # capability — the AI gets a separate session_token after pairing.
    issuer = TokenIssuer(
        issuer_id=runtime.issuer_id,
        signing_key=runtime._token_private,  # noqa: SLF001
        public_key=runtime._token_public,  # noqa: SLF001
    )
    session_manager = SessionManager(
        issuer=issuer,
        max_sessions=args.max_sessions,
    )

    # Bootstrap token: long-lived, NO grants (it's only used for /agent/session/request
    # which checks token_id match, not grants).
    from datetime import timedelta
    bootstrap_token = issuer.issue(
        subject="bootstrap",
        grants=[],  # no tool capabilities — only used for pairing
        ttl=timedelta(days=30),
    )

    browser_session_id = f"browser-{secrets.token_hex(4)}"

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    print("=" * 70, file=sys.stderr)
    print("Adroid runtime ready.", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Web UI:          http://localhost:{args.port}/ui", file=sys.stderr)
    print(f"  Agent API:       http://localhost:{args.port}/agent/call", file=sys.stderr)
    print(f"  Pairing API:     http://localhost:{args.port}/agent/session/request", file=sys.stderr)
    print(f"  WebSocket:       ws://localhost:{args.port}/ws", file=sys.stderr)
    print(f"  Audit log:       {args.audit_log}", file=sys.stderr)
    print(f"  Bridge:          {args.bridge}", file=sys.stderr)
    print(f"  Issuer ID:       {runtime.issuer_id}", file=sys.stderr)
    print(f"  Browser session: {browser_session_id}", file=sys.stderr)
    print(f"  Max sessions:    {args.max_sessions}", file=sys.stderr)
    print(file=sys.stderr)
    print("Bootstrap token (give this to your AI agent):", file=sys.stderr)
    print(file=sys.stderr)
    print(bootstrap_token.model_dump_json(), file=sys.stderr)
    print(file=sys.stderr)
    print("Agent flow:", file=sys.stderr)
    print("  1. AI POSTs bootstrap_token + agent_id to /agent/session/request", file=sys.stderr)
    print("  2. AI gets back pairing_url → shares it with you in chat", file=sys.stderr)
    print("  3. You open the URL → pick TTL → Approve", file=sys.stderr)
    print("  4. AI polls /agent/session/{pairing_id} → gets session_token", file=sys.stderr)
    print("  5. AI calls /agent/call freely with session_token", file=sys.stderr)
    print("  6. Click Disconnect in browser to revoke", file=sys.stderr)
    print(file=sys.stderr)
    print("To expose to the internet: cloudflared tunnel --url http://localhost:" + str(args.port), file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    try:
        from adroid.web.server import run_server
    except ImportError:
        sys.stderr.write("web extras not installed. Install with: pip install adroid[web]\n")
        sys.exit(2)

    run_server(
        runtime=runtime,
        session_manager=session_manager,
        issuer=issuer,
        bootstrap_token=bootstrap_token,
        host=args.host,
        port=args.port,
        browser_session_id=browser_session_id,
    )


if __name__ == "__main__":
    main()
