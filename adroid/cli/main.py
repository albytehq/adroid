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
def version_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON (machine-readable)."),
) -> None:
    """Show version + environment info."""
    import platform
    import shutil

    from adroid.cli.pair import adb_version
    from adroid.cli.utils import print_stdout, set_json_mode

    set_json_mode(json_output)

    # Get adb version safely (don't die if not found)
    import shutil as _shutil
    adb_path = _shutil.which("adb")
    adb_v = None
    if adb_path:
        try:
            import subprocess as _sp
            proc = _sp.run([adb_path, "version"], capture_output=True, timeout=5, check=False)
            if proc.returncode == 0 and proc.stdout:
                adb_v = proc.stdout.decode("utf-8", "replace").splitlines()[0]
        except Exception:
            pass

    def _check(mod: str) -> bool:
        try:
            __import__(mod)
            return True
        except ImportError:
            return False

    data = {
        "adroid_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "adb": adb_v or None,
        "adb_path": adb_path,
        "extras_web": _check("fastapi"),
        "extras_mcp": _check("mcp"),
    }

    if json_output:
        print_stdout(data)
        return

    # Human-readable: compact table
    table = Table(show_header=False, box=None, padding=(0, 1), width=60)
    table.add_column("key", style="dim", width=14)
    table.add_column("value", style="white")
    table.add_row("Adroid", __version__)
    table.add_row("Python", sys.version.split()[0])
    table.add_row("Platform", platform.platform())
    table.add_row("adb", adb_v or "[red]not found[/red]")
    table.add_row("extras:web", "[green]yes[/green]" if data["extras_web"] else "[dim]no[/dim]")
    table.add_row("extras:mcp", "[green]yes[/green]" if data["extras_mcp"] else "[dim]no[/dim]")

    console.print(Panel(table, title="[bold blue]Adroid Version[/bold blue]", border_style="blue", padding=(0, 1), width=60))


@app.command("doctor")
def doctor_cmd(
    json_output: bool = typer.Option(False, "--json", help="Output as JSON (machine-readable)."),
) -> None:
    """Diagnose environment + dependencies."""
    import platform
    import socket
    import shutil

    from adroid.cli.pair import adb_version
    from adroid.cli.utils import print_stdout, set_json_mode

    set_json_mode(json_output)

    py_version = sys.version_info
    py_ok = py_version >= (3, 10)

    adb_path = shutil.which("adb")
    adb_ok = adb_path is not None

    try:
        import fastapi  # noqa: F401
        web_ok = True
    except ImportError:
        web_ok = False

    try:
        import mcp  # noqa: F401
        mcp_ok = True
    except ImportError:
        mcp_ok = False

    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3).close()
        net_ok = True
    except OSError:
        net_ok = False

    results = {
        "python_ok": py_ok,
        "python_version": f"{py_version.major}.{py_version.minor}.{py_version.micro}",
        "adb_ok": adb_ok,
        "adb_path": adb_path,
        "extras_web": web_ok,
        "extras_mcp": mcp_ok,
        "network_ok": net_ok,
        "all_ok": py_ok and adb_ok and web_ok and net_ok,
    }

    if json_output:
        print_stdout(results)
        return

    # Human-readable: compact
    from adroid.cli.utils import console, success, warn, info as _info
    from rich.panel import Panel

    console.print()
    if py_ok:
        success(f"Python {py_version.major}.{py_version.minor}.{py_version.micro}")
    else:
        die(f"Python {py_version.major}.{py_version.minor} is too old. Need >=3.10.")

    if adb_ok:
        success(f"adb: {adb_path}")
    else:
        warn("adb not found in PATH")
        _info("  Install: sudo apt install adb (Linux) / brew install android-tools (macOS)")

    if web_ok:
        success("extras [web]: installed")
    else:
        warn("extras [web]: not installed - pip install 'adroid[web]'")

    if mcp_ok:
        success("extras [mcp]: installed")
    else:
        _info("extras [mcp]: not installed (optional)")

    if net_ok:
        success("Network: can reach internet")
    else:
        warn("Network: cannot reach 8.8.8.8")

    console.print()
    if results["all_ok"]:
        console.print(Panel(
            "[green]All checks passed.[/green] Ready to run:\n"
            "  [cyan]adroid pair list[/cyan]   - see devices\n"
            "  [cyan]adroid start[/cyan]       - boot runtime",
            border_style="green", padding=(0, 1), width=60,
        ))
    else:
        console.print(Panel(
            "[yellow]Some checks failed. Fix the warnings above.[/yellow]",
            border_style="yellow", padding=(0, 1), width=60,
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
