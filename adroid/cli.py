"""``adroid-start`` CLI — boot the runtime + web server + (optional) bridge.

Usage::

    # Mock bridge (no phone needed — for dev / demo)
    adroid-start --bridge mock --port 7654

    # Termux bridge (run inside Termux on the phone)
    adroid-start --bridge termux --port 7654

    # ADB bridge (laptop controlling a USB-connected phone)
    adroid-start --bridge adb --port 7654 --adb-path /usr/bin/adb

After startup:
    1. Open http://localhost:7654/ui in your browser.
    2. Use ``adroid-issue-token`` to mint a token for an AI agent.
    3. The agent POSTs to http://localhost:7654/agent/call with the token.

For remote access (so an AI agent on the internet can reach your runtime),
expose port 7654 via ngrok or cloudflared.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import sys
from pathlib import Path

from adroid.bridge.adb import AdbBridge, LocalBlobStore
from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.permissions.interactive import ApprovalBroker, InteractiveGate
from adroid.runtime.core import AdroidRuntime


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="adroid-start",
        description="Start the Adroid runtime + web permission UI.",
    )
    parser.add_argument(
        "--bridge",
        choices=["mock", "termux", "adb"],
        default="mock",
        help="Device bridge to use. Default: mock (no real device).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=7654,
        help="HTTP port for web UI + agent API. Default: 7654.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address. Default 0.0.0.0 (all interfaces).",
    )
    parser.add_argument(
        "--audit-log",
        default="./adroid.auditlog",
        help="Path to the audit log file. Default ./adroid.auditlog.",
    )
    parser.add_argument(
        "--blob-store-dir",
        default="./adroid_blobs",
        help="Directory for screenshot blobs (ADB/Termux bridges). Default ./adroid_blobs.",
    )
    parser.add_argument(
        "--adb-path",
        default=None,
        help="Path to adb binary (ADB bridge only). Defaults to PATH lookup.",
    )
    parser.add_argument(
        "--issuer-id",
        default=None,
        help="Runtime issuer ID. Defaults to random.",
    )
    parser.add_argument(
        "--approval-timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for interactive approval before denying. Default 60.",
    )
    parser.add_argument(
        "--require-approval-for-read",
        action="store_true",
        help="Require interactive approval even for READ scope (default: only ACT/ADMIN).",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # ------------------------------------------------------------------
    # Construct bridge + blob store
    # ------------------------------------------------------------------

    if args.bridge == "mock":
        blob_store = InMemoryBlobStore()
        bridge = MockBridge(blob_store=blob_store)
        # Pre-populate one mock device so the runtime is usable out of the box
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
                "         Most commands will fail. Did you mean --bridge mock?\n"
            )
    elif args.bridge == "adb":
        blob_store = LocalBlobStore(args.blob_store_dir)
        bridge = AdbBridge(blob_store=blob_store, adb_path=args.adb_path)
    else:
        sys.stderr.write(f"unknown bridge: {args.bridge}\n")
        sys.exit(2)

    # ------------------------------------------------------------------
    # Construct runtime (gives us token issuer + audit + standard gate)
    # ------------------------------------------------------------------

    runtime = AdroidRuntime(
        issuer_id=args.issuer_id,
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path(args.audit_log),
    )

    # ------------------------------------------------------------------
    # Construct interactive gate + broker (shares runtime's keys)
    # ------------------------------------------------------------------

    broker = ApprovalBroker()
    interactive_gate = InteractiveGate(
        issuer_id=runtime.issuer_id,
        public_key=runtime._token_public,  # noqa: SLF001 — same-package
        broker=broker,
        approval_timeout_seconds=args.approval_timeout,
        require_approval_for_read=args.require_approval_for_read,
    )

    # ------------------------------------------------------------------
    # Construct a "blessed" token for the browser session owner
    # ------------------------------------------------------------------
    # v0.1.0 simplicity: print a starter agent token to stderr so the
    # operator can immediately test the agent API without running
    # `adroid-issue-token` separately. Real deployments revoke this after
    # issuing proper per-agent tokens.
    from adroid.contract.types import Capability, Scope
    starter_token = runtime.issue_token(
        subject="starter-agent",
        grants=[(cap, Scope.ACT) for cap in [
            Capability.DEVICE_LIST,
            Capability.DEVICE_OBSERVE,
            Capability.DEVICE_SCREENSHOT,
            Capability.APP_LIST,
            Capability.APP_LAUNCH,
        ]],
        ttl_seconds=86400,  # 24h
    )

    browser_session_id = f"browser-{secrets.token_hex(4)}"

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    print("=" * 70, file=sys.stderr)
    print("Adroid runtime ready.", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  Web UI:        http://localhost:{args.port}/ui", file=sys.stderr)
    print(f"  Agent API:     http://localhost:{args.port}/agent/call", file=sys.stderr)
    print(f"  WebSocket:     ws://localhost:{args.port}/ws", file=sys.stderr)
    print(f"  Audit log:     {args.audit_log}", file=sys.stderr)
    print(f"  Bridge:        {args.bridge}", file=sys.stderr)
    print(f"  Issuer ID:     {runtime.issuer_id}", file=sys.stderr)
    print(f"  Session ID:    {browser_session_id}", file=sys.stderr)
    print(file=sys.stderr)
    print("Starter agent token (24h TTL, ACT scope on all 5 tools):", file=sys.stderr)
    print(file=sys.stderr)
    print(starter_token.model_dump_json(), file=sys.stderr)
    print(file=sys.stderr)
    print("To expose to the internet: ngrok http " + str(args.port), file=sys.stderr)
    print("=" * 70, file=sys.stderr)

    # Lazy import so non-web installs don't fail
    try:
        from adroid.web.server import run_server
    except ImportError as exc:
        sys.stderr.write(
            "web extras not installed. Install with: pip install adroid[web]\n"
        )
        sys.exit(2)

    run_server(
        runtime=runtime,
        broker=broker,
        interactive_gate=interactive_gate,
        host=args.host,
        port=args.port,
        browser_session_id=browser_session_id,
    )


if __name__ == "__main__":
    main()
