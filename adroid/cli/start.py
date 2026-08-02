"""`adroid start` — boot the runtime + web server.

This is a single command (not a Typer sub-app), so `adroid start --help`
shows the start options directly without an extra nesting level.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path
from typing import Optional

import typer

from adroid.cli.utils import die, console
from rich.panel import Panel
from rich.table import Table


def start_cmd(
    bridge: str = typer.Option(
        "mock",
        "--bridge",
        "-b",
        help="Device bridge: mock (no device), adb (USB/WiFi), or termux (on phone)",
    ),
    port: int = typer.Option(7654, "--port", "-p", help="HTTP port for web UI + agent API"),
    host: str = typer.Option("0.0.0.0", "--host", help="Bind address (0.0.0.0 = all interfaces)"),
    audit_log: Path = typer.Option(
        Path("adroid.auditlog"),
        "--audit-log",
        help="Path to audit log file",
    ),
    blob_store_dir: Path = typer.Option(
        Path("adroid_blobs"),
        "--blob-store-dir",
        help="Directory for screenshot blobs (ADB/Termux bridges)",
    ),
    adb_path: Optional[str] = typer.Option(
        None, "--adb-path", help="Path to adb binary (ADB bridge only). Defaults to PATH lookup."
    ),
    issuer_id: Optional[str] = typer.Option(
        None, "--issuer-id", help="Runtime issuer ID. Defaults to random."
    ),
    max_sessions: int = typer.Option(
        20, "--max-sessions", help="Max concurrent AI sessions"
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Start the Adroid runtime + web pairing UI.

    After startup:
      1. Open http://localhost:PORT/ui in your browser
      2. The runtime prints a bootstrap_token — give it to your AI agent
      3. AI calls /agent/session/request to start pairing
      4. Approve in browser → AI gets full access until TTL/disconnect

    \b
    Examples:
      adroid start --bridge mock             # no device (demo/dev)
      adroid start --bridge adb              # USB or wireless device
      adroid start --bridge adb --port 8080  # custom port
    """
    import logging

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
            from adroid.cli.utils import warn
            warn("You selected --bridge termux but we don't appear to be inside Termux.")
            warn("Most commands will fail. Did you mean --bridge mock or --bridge adb?")
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
    bootstrap_token = issuer.issue(
        subject="bootstrap",
        grants=[],  # no tool capabilities — only used for pairing
        ttl=timedelta(days=30),
    )
    browser_session_id = f"browser-{secrets.token_hex(4)}"

    # ------------------------------------------------------------------
    # Print startup banner
    # ------------------------------------------------------------------

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", width=18)
    table.add_column("value", style="white")
    table.add_row("Web UI", f"http://localhost:{port}/ui")
    table.add_row("Agent API", f"http://localhost:{port}/agent/call")
    table.add_row("Pairing API", f"http://localhost:{port}/agent/session/request")
    table.add_row("WebSocket", f"ws://localhost:{port}/ws")
    table.add_row("Audit log", str(audit_log))
    table.add_row("Bridge", bridge)
    table.add_row("Issuer ID", runtime.issuer_id)
    table.add_row("Browser session", browser_session_id)
    table.add_row("Max sessions", str(max_sessions))

    console.print(Panel(
        table,
        title="[bold blue]Adroid Runtime Ready[/bold blue]",
        border_style="blue",
        padding=(1, 2),
    ))

    console.print()
    console.print(Panel(
        f"[bold]Bootstrap token[/bold] (give this to your AI agent):\n\n"
        f"[cyan]{bootstrap_token.model_dump_json()}[/cyan]",
        border_style="yellow",
        padding=(1, 2),
    ))

    console.print()
    console.print(Panel(
        "[bold]Agent flow:[/bold]\n"
        "[dim]1.[/dim] AI POSTs bootstrap_token + agent_id to /agent/session/request\n"
        "[dim]2.[/dim] AI gets back pairing_url → shares it with you in chat\n"
        "[dim]3.[/dim] You open the URL → pick TTL → Approve\n"
        "[dim]4.[/dim] AI polls /agent/session/{pairing_id} → gets session_token\n"
        "[dim]5.[/dim] AI calls /agent/call freely with session_token\n"
        "[dim]6.[/dim] Click Disconnect in browser to revoke\n\n"
        f"[dim]To expose to the internet:[/dim]\n"
        f"  [cyan]cloudflared tunnel --url http://localhost:{port}[/cyan]",
        border_style="green",
        padding=(1, 2),
    ))

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


# Backwards-compat entry point used by old `adroid-start` script
def start_main() -> None:
    """Legacy entry point — preserved for backwards compat with the old
    `adroid-start` script. Calls start_cmd with sys.argv parsed by Typer."""
    import typer as _typer
    _app = _typer.Typer()
    _app.command()(start_cmd)
    _app()


if __name__ == "__main__":
    start_main()
