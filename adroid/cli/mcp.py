"""`adroid mcp` — MCP server subcommands."""

from __future__ import annotations

import typer

from adroid.cli.utils import die, info, warn

app = typer.Typer(
    name="mcp",
    help="MCP server integration (optional, requires adroid[mcp] install).",
    no_args_is_help=True,
    rich_markup_mode="rich",
)


@app.command("serve")
def serve_cmd(
    audit_log: str = typer.Option(..., "--audit-log", help="Path to audit log file"),
    blob_store_dir: str = typer.Option(
        "./adroid_blobs", "--blob-store-dir", help="Directory for screenshot blobs"
    ),
    adb_path: str = typer.Option(None, "--adb-path", help="Path to adb binary"),
    issuer_id: str = typer.Option(None, "--issuer-id", help="Runtime issuer ID"),
    mock: bool = typer.Option(False, "--mock", help="Use mock bridge (no real device)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """Run the MCP server (stdio transport) for AI agent integration.

    This exposes Adroid tools over the Model Context Protocol. Install with:
        pip install 'adroid[mcp]'

    Point any MCP-capable client (Claude Desktop, etc.) at this process
    via stdio transport.
    """
    try:
        from adroid.mcp.server import main as _mcp_main
    except ImportError:
        die(
            "MCP extras not installed. Install with:\n"
            "  pip install 'adroid[mcp]'",
            exit_code=2,
        )

    # Build args and delegate to the existing mcp.server:main
    import sys
    argv = ["adroid-mcp-server", "--audit-log", audit_log, "--blob-store-dir", blob_store_dir]
    if adb_path:
        argv.extend(["--adb-path", adb_path])
    if issuer_id:
        argv.extend(["--issuer-id", issuer_id])
    if mock:
        argv.append("--mock")
    if verbose:
        argv.append("--verbose")
    sys.argv = argv
    _mcp_main()


if __name__ == "__main__":
    app()
