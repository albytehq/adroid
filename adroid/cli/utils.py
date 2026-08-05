"""Shared utilities for Adroid CLI commands.

Design principles (research-based, per CLI Guidelines + Arcjet blog):

1. **stdout = data, stderr = human-readable info.**
   Machine-readable output (token JSON, audit events) goes to stdout
   via `print_stdout()`. Human-readable panels/tables go to stderr via
   `console` (Rich). This lets users pipe data:
     TOKEN=$(adroid start --print-token)
     adroid audit show --json | jq '.[] | .method'

2. **Constrained panel width.**
   Rich panels default to full terminal width. On wide monitors this
   looks terrible and makes copy-paste harder. We cap at 100 chars
   (industry standard for terminal readability, per CLI Guidelines).

3. **--json flag support.**
   Every command that returns data should support `--json` for
   machine-readable output. This is the #1 recommendation from
   "Designing CLIs for AI Agents" (Christian F. Jung, 2026).

4. **--quiet / -q flag.**
   Suppress non-essential output. Only show errors + final result.

5. **No box-drawing chars on data output.**
   Rich panels use `│`, `╭`, `╰` which break copy-paste. Data output
   uses `print()` (raw, no formatting).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

# ---------------------------------------------------------------------------
# Console instances
# ---------------------------------------------------------------------------

# Human-readable console (stderr) — panels, tables, colors.
# Width constrained to max 100 chars for readability (CLI Guidelines).
_MAX_WIDTH = 100
try:
    _term_width = os.get_terminal_size().columns
except (OSError, ValueError):
    _term_width = 80
_console_width = min(_term_width, _MAX_WIDTH)

# console writes to stderr — stdout is reserved for machine-readable output
# (JSON, raw tokens) so `adroid token issue ... | jq` works cleanly.
console = Console(stderr=True, width=_console_width)
err_console = Console(stderr=True, style="bold red", width=_console_width)

# Quiet mode flag — set by --quiet flag on commands
_quiet_mode = False

# JSON mode flag — set by --json flag on commands
_json_mode = False


def set_quiet(value: bool) -> None:
    """Enable/disable quiet mode (suppress non-essential output)."""
    global _quiet_mode
    _quiet_mode = value


def set_json_mode(value: bool) -> None:
    """Enable/disable JSON output mode (machine-readable to stdout)."""
    global _json_mode
    _json_mode = value


def is_json_mode() -> bool:
    return _json_mode


# ---------------------------------------------------------------------------
# Data output (stdout — raw, no formatting, pipeable)
# ---------------------------------------------------------------------------


def print_stdout(data: str | dict | list) -> None:
    """Print data to stdout. No Rich, no panels, no colors.

    Use this for machine-readable output (token JSON, audit events, etc.).
    Safe to pipe: `TOKEN=$(adroid start --print-token)` captures this.

    If data is dict/list, it's JSON-serialized with indent=2.
    """
    if isinstance(data, (dict, list)):
        print(json.dumps(data, indent=2, default=str))
    else:
        print(data)


# ---------------------------------------------------------------------------
# Human-readable output (stderr — Rich panels, colors)
# ---------------------------------------------------------------------------


def info(msg: str, **kwargs: Any) -> None:
    """Print an informational message (suppressed in quiet mode)."""
    if not _quiet_mode:
        console.print(f"[blue]i[/blue] {msg}", **kwargs)


def success(msg: str, **kwargs: Any) -> None:
    """Print a success message in green."""
    console.print(f"[green]v[/green] {msg}", **kwargs)


def warn(msg: str, **kwargs: Any) -> None:
    """Print a warning message in yellow."""
    console.print(f"[yellow]![/yellow] {msg}", **kwargs)


def error(msg: str, **kwargs: Any) -> None:
    """Print an error message in red to stderr."""
    err_console.print(f"X {msg}", **kwargs)


def die(msg: str, exit_code: int = 1) -> None:
    """Print an error and exit."""
    error(msg)
    sys.exit(exit_code)


def header(title: str, subtitle: str | None = None) -> None:
    """Print a styled section header (suppressed in quiet mode)."""
    if _quiet_mode:
        return
    body = f"[bold]{title}[/bold]"
    if subtitle:
        body += f"\n[dim]{subtitle}[/dim]"
    console.print(Panel(body, border_style="blue", padding=(0, 2)))


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run_adb(args: list[str], *, timeout: float = 15.0, check: bool = False) -> tuple[int, str, str]:
    """Run an adb command. Returns (returncode, stdout, stderr).

    Finds the adb binary via PATH. Dies with a friendly message if not found.
    """
    adb_path = shutil.which("adb") or "adb"
    try:
        proc = subprocess.run(
            [adb_path] + args,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        die(
            "adb binary not found. Install with:\n"
            "  Debian/Ubuntu: sudo apt install adb\n"
            "  macOS:         brew install android-tools\n"
            "  Windows:       download platform-tools from developer.android.com",
            exit_code=2,
        )
    return (
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def adb_version() -> str | None:
    """Return the adb version string, or None if adb is not available."""
    rc, out, _ = run_adb(["version"], timeout=5.0)
    if rc != 0:
        return None
    return out.splitlines()[0] if out else None


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def default_audit_log_path() -> Path:
    return Path.cwd() / "adroid.auditlog"


def default_blob_store_dir() -> Path:
    return Path.cwd() / "adroid_blobs"


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def ask(prompt: str, default: str | None = None) -> str:
    """Prompt the user for input."""
    return Prompt.ask(prompt, default=default)


def confirm(prompt: str, default: bool = False) -> bool:
    """Yes/no confirmation prompt."""
    return Confirm.ask(prompt, default=default)
