"""`adroid token` — capability token issuance utilities."""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.table import Table

from adroid.cli.utils import die, header, console

app = typer.Typer(
    name="token",
    help="Issue and inspect capability tokens.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("issue")
def issue_cmd(
    audit_log: Path = typer.Option(
        Path("adroid.auditlog"),
        "--audit-log",
        help="Runtime's audit log path (used to find the signing key)",
    ),
    subject: str = typer.Option(..., "--subject", "-s", help="Token subject (e.g. 'agent:foo')"),
    grant: list[str] = typer.Option(
        ...,
        "--grant",
        "-g",
        help="Capability:scope, e.g. 'device.list:read'. May be repeated.",
    ),
    ttl: int = typer.Option(3600, "--ttl", help="Token TTL in seconds (default 3600)"),
    issuer_id: str = typer.Option(
        "adroid-cli", "--issuer-id", help="Issuer ID (must match runtime)"
    ),
    print_env: bool = typer.Option(
        False, "--print-env", help="Also print 'export ADROID_TOKEN=...' for shell sourcing"
    ),
) -> None:
    """Issue a signed capability token.

    Examples:
      adroid token issue --subject agent:foo --grant device.list:read --grant device.observe:read
      adroid token issue --subject agent:bar --grant app.launch:act --ttl 86400
    """
    from adroid.contract.types import Capability, CapabilityGrant, Scope
    from adroid.permissions.tokens import (
        TokenIssuer,
        generate_signing_keypair,
        load_private_key,
        load_public_key,
        serialize_private_key,
        serialize_public_key,
    )

    def _key_path(audit_log: Path) -> Path:
        return Path(str(audit_log) + ".tokenkey")

    def _load_or_create_keypair(audit_log: Path):
        kp_path = _key_path(audit_log)
        pub_path = Path(str(kp_path) + ".pub")
        if kp_path.exists() and pub_path.exists():
            private = load_private_key(kp_path.read_text())
            public = load_public_key(pub_path.read_text())
        else:
            private, public = generate_signing_keypair()
            kp_path.write_text(serialize_private_key(private), encoding="ascii")
            pub_path.write_text(serialize_public_key(public), encoding="ascii")
            try:
                kp_path.chmod(0o600)
            except OSError:
                pass  # Windows
        return private, public

    def _parse_grant(spec: str) -> CapabilityGrant:
        if ":" not in spec:
            die(f"Grant must be 'capability:scope', got {spec!r}")
        cap_str, scope_str = spec.split(":", 1)
        try:
            cap = Capability(cap_str.strip())
        except ValueError as exc:
            die(f"Unknown capability: {cap_str} ({exc})")
        try:
            scope = Scope(scope_str.strip())
        except ValueError:
            die(f"Unknown scope: {scope_str} (must be read|act|admin)")
        return CapabilityGrant(capability=cap, scope=scope)

    private, public = _load_or_create_keypair(audit_log)
    issuer = TokenIssuer(issuer_id=issuer_id, signing_key=private, public_key=public)
    grants = [_parse_grant(g) for g in grant]

    from datetime import timedelta
    token = issuer.issue(subject=subject, grants=grants, ttl=timedelta(seconds=ttl))

    header("Issued Token", f"Subject: {subject} · TTL: {ttl}s · Grants: {len(grants)}")

    # Print token JSON to stdout (so it can be piped)
    console.print(token.model_dump_json())
    console.print()

    # Print summary table
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Capability", style="white")
    table.add_column("Scope", style="white")
    for g in grants:
        table.add_row(g.capability.value, g.scope.value)
    console.print(table)

    if print_env:
        hex_token = token.model_dump_json().encode("utf-8").hex()
        console.print("\n[dim]Source this with:[/dim]", file=sys.stderr)
        console.print(f'[cyan]export ADROID_TOKEN="{hex_token}"[/cyan]', file=sys.stderr)


@app.command("inspect")
def inspect_cmd(
    token_json: str = typer.Argument(..., help="Token JSON (or '-' for stdin)"),
) -> None:
    """Decode and display a token's contents (without verifying signature).

    Useful for debugging — see what subject, grants, expiry a token has.
    """
    from adroid.contract.types import CapabilityToken

    if token_json == "-":
        token_json = sys.stdin.read().strip()

    try:
        token = CapabilityToken.model_validate_json(token_json)
    except Exception as exc:
        die(f"Failed to parse token: {exc}")

    header("Token Inspection")
    console.print(f"[dim]token_id:[/dim]    [white]{token.token_id}[/white]")
    console.print(f"[dim]issuer:[/dim]      [white]{token.issuer}[/white]")
    console.print(f"[dim]subject:[/dim]     [white]{token.subject}[/white]")
    console.print(f"[dim]issued_at:[/dim]   [white]{token.issued_at.isoformat()}[/white]")
    console.print(f"[dim]expires_at:[/dim]  [white]{token.expires_at.isoformat()}[/white]")
    console.print(f"[dim]signature:[/dim]   [white]{token.signature[:32]}…[/white]")
    console.print()
    console.print("[bold]Grants:[/bold]")
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Capability", style="white")
    table.add_column("Scope", style="white")
    table.add_column("Rate limit", style="dim")
    for g in token.grants:
        rl = f"{g.rate_limit_per_minute}/min" if g.rate_limit_per_minute else "default"
        table.add_row(g.capability.value, g.scope.value, rl)
    console.print(table)


if __name__ == "__main__":
    app()
