"""Token issuance CLI — generate a signed capability token for an integrator.

Usage::

    # Generate keys + issue a token, prints the token JSON to stdout
    python -m adroid.mcp.issue_token \\
        --audit-log /var/log/adroid.auditlog \\
        --subject "agent:foo" \\
        --grant device.list:read \\
        --grant device.observe:read \\
        --ttl 3600 \\
        --print-env

The first invocation also bootstraps the runtime's token-signing keypair
(it is derived deterministically from the audit log path's sibling key
file). Subsequent invocations reuse the existing keypair.

For v0.1.0 we serialize the keypair to ``<audit_log>.tokenkey`` in PKCS8
PEM.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from adroid.contract.types import Capability, CapabilityGrant, Scope
from adroid.permissions.tokens import (
    load_private_key,
    load_public_key,
    serialize_private_key,
    serialize_public_key,
)


def _key_path(audit_log: Path) -> Path:
    return Path(str(audit_log) + ".tokenkey")


def _load_or_create_keypair(audit_log: Path):
    from adroid.permissions.tokens import generate_signing_keypair

    kp_path = _key_path(audit_log)
    pub_path = Path(str(kp_path) + ".pub")
    if kp_path.exists() and pub_path.exists():
        private = load_private_key(kp_path.read_text())
        public = load_public_key(pub_path.read_text())
    else:
        private, public = generate_signing_keypair()
        kp_path.write_text(serialize_private_key(private), encoding="ascii")
        pub_path.write_text(serialize_public_key(public), encoding="ascii")
        # Lock down private key perms
        try:
            kp_path.chmod(0o600)
        except OSError:
            pass  # Windows
    return private, public


def _parse_grant(spec: str) -> CapabilityGrant:
    """Parse 'device.list:read' or 'app.launch:act' into a CapabilityGrant."""
    if ":" not in spec:
        raise ValueError(f"grant must be 'capability:scope', got {spec!r}")
    cap_str, scope_str = spec.split(":", 1)
    try:
        cap = Capability(cap_str.strip())
    except ValueError as exc:
        raise ValueError(f"unknown capability: {cap_str}") from exc
    try:
        scope = Scope(scope_str.strip())
    except ValueError as exc:
        raise ValueError(f"unknown scope: {scope_str} (must be read|act|admin)") from exc
    return CapabilityGrant(capability=cap, scope=scope)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="adroid-issue-token",
        description="Issue a signed Adroid capability token.",
    )
    parser.add_argument("--audit-log", required=True, help="Path to the runtime's audit log file.")
    parser.add_argument("--subject", required=True, help="Token subject (e.g. 'agent:foo').")
    parser.add_argument(
        "--grant",
        action="append",
        required=True,
        help="Capability:scope, e.g. 'device.list:read'. May be repeated.",
    )
    parser.add_argument("--ttl", type=int, default=3600, help="Token TTL in seconds. Default 3600.")
    parser.add_argument(
        "--issuer-id",
        default="adroid-cli",
        help="Issuer ID. Must match what the runtime expects. Default 'adroid-cli'.",
    )
    parser.add_argument(
        "--print-env",
        action="store_true",
        help="Also print 'export ADROID_TOKEN=...' for shell sourcing.",
    )
    args = parser.parse_args()

    audit_log = Path(args.audit_log)
    private, public = _load_or_create_keypair(audit_log)

    from adroid.permissions.tokens import TokenIssuer

    issuer = TokenIssuer(issuer_id=args.issuer_id, signing_key=private, public_key=public)
    grants = [_parse_grant(g) for g in args.grant]

    from datetime import timedelta
    token = issuer.issue(subject=args.subject, grants=grants, ttl=timedelta(seconds=args.ttl))

    token_json = token.model_dump_json()
    print(token_json)
    if args.print_env:
        # Hex-encode for safe shell embedding
        hex_token = token_json.encode("utf-8").hex()
        print(f"\n# Source this with: eval \"$(python -m adroid.mcp.issue_token ... --print-env)\"", file=sys.stderr)
        print(f'export ADROID_TOKEN="{hex_token}"', file=sys.stderr)


if __name__ == "__main__":
    main()
