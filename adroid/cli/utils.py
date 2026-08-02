"""Shared utilities for Adroid CLI commands."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

console = Console()
err_console = Console(stderr=True, style="bold red")


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------


def info(msg: str, **kwargs: Any) -> None:
    """Print an informational message in blue."""
    console.print(f"[blue]ℹ[/blue] {msg}", **kwargs)


def success(msg: str, **kwargs: Any) -> None:
    """Print a success message in green."""
    console.print(f"[green]✓[/green] {msg}", **kwargs)


def warn(msg: str, **kwargs: Any) -> None:
    """Print a warning message in yellow."""
    console.print(f"[yellow]⚠[/yellow] {msg}", **kwargs)


def error(msg: str, **kwargs: Any) -> None:
    """Print an error message in red to stderr."""
    err_console.print(f"✗ {msg}", **kwargs)


def die(msg: str, exit_code: int = 1) -> None:
    """Print an error and exit."""
    error(msg)
    sys.exit(exit_code)


def header(title: str, subtitle: str | None = None) -> None:
    """Print a styled section header."""
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
