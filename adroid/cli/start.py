"""`adroid start` — boot the runtime + web server.

Output convention (research-based, per CLI Guidelines):
  - Token JSON → stdout (raw, no Rich panel, pipeable)
  - Human-readable info → stderr (Rich panels, constrained width)
  - --print-token flag: ONLY token JSON to stdout
  - --json flag: all output as JSON to stdout
  - --quiet flag: suppress non-essential stderr output

Usage:
  # Human mode (default) — panels to stderr, token to stdout
  adroid start --bridge adb

  # Pipe token directly
  TOKEN=$(adroid start --bridge adb --print-token 2>/dev/null)

  # JSON mode (for scripts/AI agents)
  adroid start --bridge adb --json

  # Quiet mode (minimal output)
  adroid start --bridge adb --quiet
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

import typer

from adroid.cli.utils import (
    die,
    print_stdout,
    set_json_mode,
    set_quiet,
    warn,
)
from rich.panel import Panel
from rich.table import Table


def start_cmd(
    bridge: str = typer.Option(
        "mock", "--bridge", "-b",
        help="Device bridge: mock (no device), adb (USB/WiFi), or termux (on phone)",
    ),
    port: int = typer.Option(7654, "--port", "-p", help="HTTP port for web UI + agent API"),
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address (default loopback; use 0.0.0.0 to expose to LAN)"),
    audit_log: Path = typer.Option(
        Path("adroid.auditlog"), "--audit-log", help="Path to audit log file",
    ),
    blob_store_dir: Path = typer.Option(
        Path("adroid_blobs"), "--blob-store-dir", help="Directory for screenshot blobs",
    ),
    adb_path: Optional[str] = typer.Option(
        None, "--adb-path", help="Path to adb binary (ADB bridge only)",
    ),
    issuer_id: Optional[str] = typer.Option(
        None, "--issuer-id", help="Runtime issuer ID. Defaults to random.",
    ),
    max_sessions: int = typer.Option(
        20, "--max-sessions", help="Max concurrent AI sessions",
    ),
    print_token: bool = typer.Option(
        False, "--print-token",
        help="Print ONLY the bootstrap token JSON to stdout (for piping). All other output goes to stderr.",
    ),
    json_output: bool = typer.Option(
        False, "--json",
        help="Output all startup info as JSON to stdout (machine-readable).",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q",
        help="Suppress non-essential output. Only show errors + token.",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Start the Adroid runtime + web pairing UI.

    After startup:
      1. Open http://localhost:PORT/ui in your browser
      2. The runtime prints a bootstrap_token — give it to your AI agent
      3. AI calls /agent/session/request to start pairing
      4. Approve in browser -> AI gets full access until TTL/disconnect

    Examples:
      adroid start --bridge mock             # no device (demo/dev)
      adroid start --bridge adb              # USB or wireless device
      adroid start --bridge adb --port 8080  # custom port
      TOKEN=$(adroid start --print-token 2>/dev/null)  # pipe token
    """
    import logging

    # Set mode flags
    set_quiet(quiet)
    set_json_mode(json_output)

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if bridge not in ("mock", "adb", "termux"):
        die(f"Unknown bridge: {bridge!r}. Must be one of: mock, adb, termux")

    # ------------------------------------------------------------------
    # Construct bridge + blob store
    # ------------------------------------------------------------------

    if bridge == "mock":
        from adroid.bridge.mock import InMemoryBlobStore, MockBridge
        from adroid.contract.types import DeviceId, DeviceInfo, DeviceState
        blob_store = InMemoryBlobStore()
        bridge_obj = MockBridge(blob_store=blob_store)
        bridge_obj.add_device(
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
    elif bridge == "termux":
        from adroid.bridge.termux import TermuxBridge
        from adroid.bridge.adb import LocalBlobStore
        blob_store = LocalBlobStore(blob_store_dir)
        bridge_obj = TermuxBridge(blob_store=blob_store)
        if not TermuxBridge.is_termux():
            warn("You selected --bridge termux but we don't appear to be inside Termux.")
    elif bridge == "adb":
        from adroid.bridge.adb import AdbBridge, LocalBlobStore
        blob_store = LocalBlobStore(blob_store_dir)
        bridge_obj = AdbBridge(blob_store=blob_store, adb_path=adb_path)

    # ------------------------------------------------------------------
    # Construct runtime
    # ------------------------------------------------------------------

    from adroid.runtime.core import AdroidRuntime
    runtime = AdroidRuntime(
        issuer_id=issuer_id,
        bridge=bridge_obj,
        blob_store=blob_store,
        audit_log_path=audit_log,
    )

    # ------------------------------------------------------------------
    # Session manager + bootstrap token
    # ------------------------------------------------------------------

    from datetime import timedelta
    from adroid.permissions.sessions import SessionManager
    from adroid.permissions.tokens import TokenIssuer

    issuer = TokenIssuer(
        issuer_id=runtime.issuer_id,
        signing_key=runtime._token_private,  # noqa: SLF001
        public_key=runtime._token_public,  # noqa: SLF001
    )
    session_manager = SessionManager(issuer=issuer, max_sessions=max_sessions)
    # Wire revocation into the gate — defense-in-depth. After this call,
    # every gate.authorize() checks session_manager.is_revoked first.
    runtime.set_revocation_checker(session_manager.is_revoked)
    bootstrap_token = issuer.issue(
        subject="bootstrap",
        grants=[],
        ttl=timedelta(days=30),
    )
    browser_session_id = f"browser-{secrets.token_hex(4)}"

    token_json = bootstrap_token.model_dump_json()

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    if json_output:
        # Machine-readable: everything to stdout as JSON
        print_stdout({
            "status": "ready",
            "web_ui": f"http://localhost:{port}/ui",
            "agent_api": f"http://localhost:{port}/agent/call",
            "pairing_api": f"http://localhost:{port}/agent/session/request",
            "websocket": f"ws://localhost:{port}/ws",
            "audit_log": str(audit_log),
            "bridge": bridge,
            "issuer_id": runtime.issuer_id,
            "browser_session": browser_session_id,
            "max_sessions": max_sessions,
            "bootstrap_token": bootstrap_token.model_dump(mode="json"),
        })
    elif print_token:
        # ONLY token to stdout, everything else to stderr
        print_stdout(token_json)
        # Minimal info to stderr (NOT stdout — pipeable)
        from rich.console import Console as _C
        _stderr = _C(stderr=True, width=80)
        _stderr.print(f"[dim]Runtime ready on port {port}. Token printed to stdout.[/dim]")
        _stderr.print(f"[dim]Dashboard: http://localhost:{port}/ui[/dim]")
        _stderr.print(f"[dim]Expose: cloudflared tunnel --url http://localhost:{port}[/dim]")
    else:
        # Default: compact human-readable output to stderr, token to stdout
        # Token to stdout (pipeable, no Rich borders)
        print_stdout(token_json)

        # Compact info panel to stderr (NOT stdout — keeps stdout clean for piping)
        from rich.console import Console as _C
        _stderr = _C(stderr=True, width=80)

        table = Table(show_header=False, box=None, padding=(0, 1), width=76)
        table.add_column("key", style="dim", width=14)
        table.add_column("value", style="white")
        table.add_row("Dashboard", f"http://localhost:{port}/ui")
        table.add_row("Agent API", f"http://localhost:{port}/agent/call")
        table.add_row("Bridge", bridge)
        table.add_row("Issuer", runtime.issuer_id)
        table.add_row("Max sessions", str(max_sessions))
        table.add_row("Audit log", str(audit_log))

        if not quiet:
            _stderr.print(Panel(
                table,
                title="[bold blue]Adroid Runtime Ready[/bold blue]",
                border_style="blue",
                padding=(0, 1),
                width=80,
            ))
            _stderr.print()
            _stderr.print("[dim]Bootstrap token (above, stdout):[/dim]")
            _stderr.print(f"[cyan]{token_json[:80]}...[/cyan]")
            _stderr.print()
            _stderr.print(f"[dim]Expose:[/dim]  cloudflared tunnel --url http://localhost:{port}")
            _stderr.print(f"[dim]Flow:[/dim]    POST /agent/session/request -> approve -> POST /agent/call")

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    try:
        from adroid.web.server import run_server
    except ImportError:
        die(
            "Web extras not installed. Install with:\n"
            "  pip install 'adroid[web]'",
            exit_code=2,
        )

    run_server(
        runtime=runtime,
        session_manager=session_manager,
        issuer=issuer,
        bootstrap_token=bootstrap_token,
        host=host,
        port=port,
        browser_session_id=browser_session_id,
    )


if __name__ == "__main__":
    import typer as _typer
    _app = _typer.Typer()
    _app.command()(start_cmd)
    _app()
