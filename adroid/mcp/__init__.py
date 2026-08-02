"""Optional MCP server layer for Adroid.

Install with: ``pip install adroid[mcp]``
"""

from adroid.mcp.issue_token import main as issue_token_main
from adroid.mcp.server import main as server_main

__all__ = ["issue_token_main", "server_main"]
