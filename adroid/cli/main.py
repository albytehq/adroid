"""Adroid — unified CLI entry point.

A single `adroid` command with subcommands (gaya git/docker/kubectl):
    adroid version              Show version + environment info
    adroid pair ...             Device pairing (USB/wireless/tcpip)
    adroid start ...            Boot the runtime + web UI
    adroid token ...            Issue + inspect capability tokens
    adroid audit ...            Inspect + verify audit log
    adroid mcp ...              MCP server (optional, needs adroid[mcp])
    adroid doctor               Diagnose environment + dependencies

Run `adroid <command> --help` for detailed help on each subcommand.
"""

from __future__ import annotations

import sys

import typer
from rich.panel import Panel
from rich.table import Table

from adroid import __version__
from adroid.cli.audit import app as audit_app
from adroid.cli.mcp import app as mcp_app
from adroid.cli.pair import app as pair_app
from adroid.cli.start import start_cmd
from adroid.cli.token import app as token_app
from adroid.cli.utils import console, die, header, info, success, warn

app = typer.Typer(
    name="adroid",
    help="Adroid — self-hosted, contract-governed runtime for AI agents on Android.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# Register sub-apps (these have their own sub-subcommands)
app.add_typer(pair_app, name="pair")
app.add_typer(token_app, name="token")
app.add_typer(audit_app, name="audit")
app.add_typer(mcp_app, name="mcp")

# Register `start` as a direct command (not a sub-app, since it has no sub-subcommands)
app.command("start")(start_cmd)


# ---------------------------------------------------------------------------
# Top-level commands
# ---------------------------------------------------------------------------


@app.command("version")
def version_cmd() -> None:
    """Show version + environment info."""
    import platform
    import shutil

    from adroid.cli.pair import adb_version

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("key", style="dim", width=18)
    table.add_column("value", style="white")
    table.add_row("Adroid version", __version__)
    table.add_row("Python", sys.version.split()[0])
    table.add_row("Platform", platform.platform())

    adb_v = adb_version()
    table.add_row("adb", adb_v or "[red]not found[/red]")

    # Check optional deps
    def _check(mod: str, label: str) -> None:
        try:
            __import__(mod)
            table.add_row(label, "[green]installed[/green]")
        except ImportError:
            table.add_row(label, "[dim]not installed[/dim]")

    _check("fastapi", "extras: web")
    _check("mcp", "extras: mcp")

    console.print(Panel(table, title="[bold blue]Adroid Environment[/bold blue]", border_style="blue", padding=(1, 2)))


@app.command("doctor")
def doctor_cmd() -> None:
    """Diagnose environment + dependencies.

    Checks:
      - Python version (must be ≥3.10)
      - adb binary availability
      - Optional extras (web, mcp)
      - Network reachability (Google DNS, for cloudflared/ngrok setup)
    """
    import platform
    import socket
    import shutil

    header("Adroid Doctor", "Running environment diagnostics…")
    console.print()

    # Python version
    py_version = sys.version_info
    if py_version >= (3, 10):
        success(f"Python {py_version.major}.{py_version.minor}.{py_version.micro} ✓")
    else:
        die(f"Python {py_version.major}.{py_version.minor} is too old. Need ≥3.10.")

    # adb
    adb_path = shutil.which("adb")
    if adb_path:
        success(f"adb binary: {adb_path}")
    else:
        warn("adb binary not found in PATH")
        info("  Install with: sudo apt install adb  (Linux)")
        info("               brew install android-tools  (macOS)")

    # Optional extras
    try:
        import fastapi  # noqa: F401
        success("extras [web]: installed")
    except ImportError:
        warn("extras [web]: not installed — run: pip install 'adroid[web]'")

    try:
        import mcp  # noqa: F401
        success("extras [mcp]: installed")
    except ImportError:
        info("extras [mcp]: not installed (optional, for MCP server mode)")

    # Network reachability
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3).close()
        success("Network: can reach internet (good for cloudflared/ngrok)")
    except OSError:
        warn("Network: cannot reach 8.8.8.8 — check your connection")

    console.print()
    console.print(Panel(
        "[bold green]Diagnostics complete.[/bold green]\n\n"
        "[dim]If everything above is green, you're ready to:[/dim]\n"
        "  [cyan]adroid pair list[/cyan]   — see connected devices\n"
        "  [cyan]adroid start[/cyan]       — boot the runtime",
        border_style="green",
        padding=(1, 2),
    ))


@app.callback()
def main(
    ctx: typer.Context,
) -> None:
    """Adroid — self-hosted, contract-governed runtime for AI agents on Android.

    A single runtime that gives AI agents a stable, audit-grade surface
    to observe, request, and act on Android devices — with one-time
    pairing so the device owner approves the AI once, then it has full
    access until they disconnect.

    \b
    Quick start:
      adroid doctor              # check your environment
      adroid pair list           # see connected devices
      adroid pair wireless ...   # pair Android 11+ device (no USB)
      adroid start --bridge adb  # boot the runtime
      adroid audit show          # inspect audit log

    \b
    Documentation:
      https://github.com/albytehq/adroid
    """
    pass


if __name__ == "__main__":
    app()
