"""Adroid CLI — unified `adroid` command with subcommands.

Submodules:
    main    — top-level entry point + `version` + `doctor` commands
    pair    — `adroid pair` device pairing subcommands
    start   — `adroid start` runtime launcher
    token   — `adroid token` token issuance + inspection
    audit   — `adroid audit` log inspection + verification
    mcp     — `adroid mcp` MCP server (optional)
    utils   — shared console + subprocess helpers
"""

from adroid.cli.main import app

__all__ = ["app"]
