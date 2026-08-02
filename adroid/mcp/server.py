"""Optional MCP server layer that exposes Adroid tools over the Model
Context Protocol.

This module imports the ``mcp`` package lazily — the runtime core has no
dependency on MCP. Install with::

    pip install adroid[mcp]

Then run::

    python -m adroid.mcp.server --audit-log /var/log/adroid.auditlog

The server reads a capability token from the ``ADROID_TOKEN`` environment
variable (hex-encoded JSON of a signed CapabilityToken). Every tool call
the server proxies through the runtime uses this token. v0.2.0 will add
per-session tokens issued via an MCP ``auth.issue`` tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from adroid.bridge.adb import AdbBridge, LocalBlobStore
from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.errors import AdroidError
from adroid.contract.types import CapabilityToken, DeviceId, Scope
from adroid.runtime.core import AdroidRuntime

log = logging.getLogger(__name__)


def _load_token() -> CapabilityToken:
    """Load the integrator's capability token from ADROID_TOKEN env var."""
    raw = os.environ.get("ADROID_TOKEN")
    if not raw:
        sys.stderr.write(
            "ADROID_TOKEN env var not set. Generate one with `python -m adroid.mcp.issue_token`.\n"
        )
        sys.exit(2)
    try:
        # The env var may be either the JSON directly, or hex-encoded JSON
        # (safer for shell quoting).
        if all(c in "0123456789abcdef" for c in raw.lower()):
            payload = bytes.fromhex(raw).decode("utf-8")
        else:
            payload = raw
        return CapabilityToken.model_validate_json(payload)
    except Exception as exc:
        sys.stderr.write(f"failed to parse ADROID_TOKEN: {exc}\n")
        sys.exit(2)


def _build_runtime(args: argparse.Namespace) -> AdroidRuntime:
    if args.mock:
        blob_store = InMemoryBlobStore()
        bridge: Any = MockBridge(blob_store=blob_store)
        # Pre-populate one mock device so the server is usable out of the box.
        from adroid.contract.types import DeviceInfo, DeviceState
        bridge.add_device(
            DeviceInfo(
                id=DeviceId(kind="adb", value="mock-emulator-5554"),
                state=DeviceState.ONLINE,
                model="MockPhone",
                manufacturer="Adroid",
                android_version="14",
                sdk_level=34,
            )
        )
    else:
        blob_store = LocalBlobStore(args.blob_store_dir)
        bridge = AdbBridge(blob_store=blob_store, adb_path=args.adb_path)

    return AdroidRuntime(
        issuer_id=args.issuer_id,
        bridge=bridge,
        blob_store=blob_store,
        audit_log_path=Path(args.audit_log),
    )


async def _serve(runtime: AdroidRuntime, token: CapabilityToken) -> None:
    """Run the MCP stdio server until cancelled."""
    try:
        from mcp.server import Server
        from mcp.server.stdio import stdio_server
        from mcp.types import (
            CallToolResult,
            ImageContent,
            TextContent,
            Tool,
        )
    except ImportError as exc:
        sys.stderr.write(
            "mcp package not installed. Install with: pip install adroid[mcp]\n"
        )
        sys.exit(2)

    server: Server = Server("adroid")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        tools: list[Tool] = []
        for spec in runtime.list_tools():
            tools.append(
                Tool(
                    name=spec.name,
                    description=spec.description,
                    inputSchema=spec.args_schema,
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent | ImageContent]:
        try:
            if name == "device.list":
                devices = runtime.list_devices(token=token)
                payload = [d.model_dump(mode="json") for d in devices]
                return [TextContent(type="text", text=json.dumps({"devices": payload}, default=str))]

            if name == "device.observe":
                did = DeviceId.model_validate(arguments["device_id"])
                info = runtime.observe_device(token=token, device_id=did)
                return [TextContent(type="text", text=info.model_dump_json())]

            if name == "device.screenshot":
                did = DeviceId.model_validate(arguments["device_id"])
                shot = runtime.capture_screenshot(token=token, device_id=did)
                png_bytes = runtime.get_blob(shot.blob_ref) or b""
                import base64
                return [
                    ImageContent(
                        type="image",
                        data=base64.b64encode(png_bytes).decode("ascii"),
                        mimeType="image/png",
                    ),
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "blob_ref": shot.blob_ref,
                                "width": shot.width,
                                "height": shot.height,
                                "device_id": str(shot.device_id),
                            }
                        ),
                    ),
                ]

            if name == "app.list":
                did = DeviceId.model_validate(arguments["device_id"])
                apps = runtime.list_installed_apps(token=token, device_id=did)
                payload = [a.model_dump(mode="json") for a in apps]
                return [TextContent(type="text", text=json.dumps({"apps": payload}, default=str))]

            if name == "app.launch":
                did = DeviceId.model_validate(arguments["device_id"])
                runtime.launch_app(
                    token=token,
                    device_id=did,
                    package_name=arguments["package_name"],
                )
                return [TextContent(type="text", text='{"launched": true}')]

            return [
                TextContent(
                    type="text",
                    text=json.dumps({"error": f"unknown tool: {name}", "code": "adroid.tool.unknown"}),
                )
            ]
        except AdroidError as exc:
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "error": str(exc),
                            "code": exc.code,
                            "details": exc.details,
                        }
                    ),
                )
            ]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="adroid-mcp-server",
        description="Adroid MCP server (stdio). Exposes Android device tools to MCP-capable AI agents.",
    )
    parser.add_argument("--audit-log", required=True, help="Path to the audit log file.")
    parser.add_argument("--blob-store-dir", default="./adroid_blobs", help="Directory for screenshot blobs.")
    parser.add_argument("--adb-path", default=None, help="Path to adb binary. Defaults to PATH lookup.")
    parser.add_argument("--issuer-id", default=None, help="Runtime issuer ID. Defaults to random.")
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Use the mock bridge (no real device needed). Useful for demos + tests.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    runtime = _build_runtime(args)
    token = _load_token()
    log.info("adroid-mcp-server starting (issuer=%s, mock=%s)", runtime.issuer_id, args.mock)
    log.info("token subject=%s, grants=%d, expires_at=%s", token.subject, len(token.grants), token.expires_at)

    try:
        asyncio.run(_serve(runtime, token))
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
