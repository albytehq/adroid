"""`adroid audit` — audit log inspection."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.table import Table

from adroid.cli.utils import console, die, header, info, success, warn

app = typer.Typer(
    name="audit",
    help="Inspect and verify the signed audit log.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("show")
def show_cmd(
    audit_log: Path = typer.Option(
        Path("adroid.auditlog"),
        "--audit-log",
        help="Path to audit log file",
    ),
    limit: int = typer.Option(20, "--limit", "-n", help="Number of recent events to show"),
    json_output: bool = typer.Option(False, "--json", help="Output as JSON instead of table"),
) -> None:
    """Show recent audit log events."""
    if not audit_log.exists():
        die(f"Audit log not found: {audit_log}")

    import json
    events: list[dict] = []
    with open(audit_log) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                events.append(rec.get("payload", rec))
            except json.JSONDecodeError:
                continue

    if not events:
        info("Audit log is empty.")
        return

    events = events[-limit:]

    if json_output:
        import json as _json
        console.print(_json.dumps(events, indent=2, default=str))
        return

    header(f"Audit Log — Last {len(events)} Events", f"File: {audit_log}")

    table = Table(show_header=True, header_style="bold cyan", show_lines=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("Time", style="white", width=12)
    table.add_column("Outcome", style="white", width=10)
    table.add_column("Method", style="white")
    table.add_column("Capability", style="dim")
    table.add_column("Subject", style="dim")

    for e in events:
        outcome = e.get("outcome", "?")
        outcome_style = {
            "success": "green",
            "denied": "yellow",
            "error": "red",
        }.get(outcome, "white")
        ts = e.get("timestamp", "")[:19].replace("T", " ")
        table.add_row(
            str(e.get("sequence", "?")),
            ts,
            f"[{outcome_style}]{outcome}[/{outcome_style}]",
            e.get("method", "?"),
            (e.get("capability") or "—"),
            (e.get("subject") or "—"),
        )

    console.print(table)


@app.command("verify")
def verify_cmd(
    audit_log: Path = typer.Option(
        Path("adroid.auditlog"),
        "--audit-log",
        help="Path to audit log file",
    ),
    public_key_pem: Optional[Path] = typer.Option(
        None,
        "--public-key",
        help="Path to PEM file with the audit-signing public key. If omitted, only hash chain is verified.",
    ),
) -> None:
    """Verify the audit log integrity (hash chain + signatures)."""
    if not audit_log.exists():
        die(f"Audit log not found: {audit_log}")

    from adroid.audit.writer import AuditReader
    from adroid.permissions.tokens import load_public_key

    public_key = None
    if public_key_pem:
        public_key = load_public_key(public_key_pem.read_text())

    reader = AuditReader(log_path=audit_log, public_key=public_key)  # type: ignore[arg-type]
    results = list(reader.verify_all())

    ok_count = sum(1 for _, ok, _ in results if ok)
    fail_count = sum(1 for _, ok, _ in results if not ok)

    header("Audit Log Verification", f"File: {audit_log}")

    if fail_count == 0:
        success(f"All {ok_count} events verified successfully.")
    else:
        warn(f"{ok_count} events OK, {fail_count} FAILED:")
        for seq, ok, err in results:
            if not ok:
                console.print(f"  [red]✗[/red] seq={seq}: {err}")

    if fail_count > 0:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
