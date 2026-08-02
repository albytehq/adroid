"""Web server + agent HTTP API for Adroid.

This module ties together:
    - The runtime (orchestrator)
    - The interactive gate (blocks ACT scope pending approval)
    - An HTTP API for AI agents to call tools
    - A browser UI for the user to approve/deny requests
    - A WebSocket for live terminal + pending-request streaming

Run with::

    adroid-start --bridge termux --port 7654

Or directly::

    python -m adroid.web.server --bridge termux --port 7654
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from adroid.bridge.mock import InMemoryBlobStore, MockBridge
from adroid.contract.errors import AdroidError
from adroid.contract.types import (
    AppInfo,
    AuditEvent,
    AuditOutcome,
    Capability,
    CapabilityToken,
    DeviceId,
    DeviceInfo,
    Scope,
    Screenshot,
)
from adroid.permissions.interactive import ApprovalBroker, InteractiveGate
from adroid.runtime.core import AdroidRuntime

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Request / response models (JSON over HTTP for agent API)
# ---------------------------------------------------------------------------


class AgentCallRequest(BaseModel):
    """Body for POST /agent/call.

    The token is the signed CapabilityToken JSON (the integrator got it
    out-of-band from the runtime operator).
    """

    token: dict[str, Any]
    tool: str  # e.g. "device.list", "device.screenshot"
    args: dict[str, Any] = {}


class AgentCallResponse(BaseModel):
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    audit_event_id: str | None = None
    approval_id: str | None = None  # present if interactive approval happened


# ---------------------------------------------------------------------------
# Terminal stream — captures agent activity for live browser display
# ---------------------------------------------------------------------------


class TerminalStream:
    """Thread-safe ring buffer of agent activity lines.

    Every tool call (start, result, error) appends a line. The web UI
    subscribes via WebSocket and receives new lines in real time.
    """

    def __init__(self, capacity: int = 500) -> None:
        self._lines: list[dict[str, Any]] = []
        self._capacity = capacity
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []

    def append(self, *, kind: str, text: str, **extra) -> None:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,  # "request" | "result" | "error" | "system"
            "text": text,
            **extra,
        }
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self._capacity:
                self._lines = self._lines[-self._capacity:]
        # Push to async subscribers
        for q in list(self._subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass  # drop if subscriber is slow

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._lines[-limit:])

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)


# ---------------------------------------------------------------------------
# Server state — single instance per process
# ---------------------------------------------------------------------------


class ServerState:
    """Holds everything the web server needs at runtime.

    Constructed once at startup, passed to FastAPI via ``app.state``.
    """

    def __init__(
        self,
        *,
        runtime: AdroidRuntime,
        broker: ApprovalBroker,
        interactive_gate: InteractiveGate,
        terminal: TerminalStream,
        browser_session_id: str,
    ):
        self.runtime = runtime
        self.broker = broker
        self.gate = interactive_gate
        self.terminal = terminal
        self.browser_session_id = browser_session_id


# ---------------------------------------------------------------------------
# Tool dispatch — the actual work
# ---------------------------------------------------------------------------


def _dispatch_tool(
    state: ServerState,
    *,
    token: CapabilityToken,
    tool: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Run a tool through the interactive gate + runtime.

    Returns the tool's result as a JSON-serializable dict.
    Raises AdroidError on permission/runtime failures (caller decides
    whether to translate to HTTP or ok=False response).
    Raises HTTPException only for unknown tools (4xx).
    """
    terminal = state.terminal

    # Map tool name -> (capability, required_scope, method_label, runtime_fn)
    tool_map = {
        "device.list": (Capability.DEVICE_LIST, Scope.READ, "list_devices"),
        "device.observe": (Capability.DEVICE_OBSERVE, Scope.READ, "observe_device"),
        "device.screenshot": (Capability.DEVICE_SCREENSHOT, Scope.READ, "capture_screenshot"),
        "app.list": (Capability.APP_LIST, Scope.READ, "list_installed_apps"),
        "app.launch": (Capability.APP_LAUNCH, Scope.ACT, "launch_app"),
    }

    if tool not in tool_map:
        raise HTTPException(
            status_code=404,
            detail=f"unknown tool: {tool}",
        )

    capability, required_scope, _method_label = tool_map[tool]
    method_label = f"runtime.{tool.replace('.', '_')}"

    # Log the request to terminal stream
    args_summary = json.dumps(args, default=str)
    terminal.append(
        kind="request",
        text=f"{tool}({args_summary})",
        subject=token.subject,
        capability=capability.value,
        scope=required_scope.value,
    )

    # Interactive approval (only for ACT/ADMIN scope). May raise
    # PermissionDeniedError on denial/timeout — let it bubble.
    approval = state.gate.authorize_interactive(
        token,
        capability,
        required_scope,
        method=method_label,
        args=args,
        device_id=args.get("device_id", {}).get("value") if isinstance(args.get("device_id"), dict) else None,
    )

    if approval:
        terminal.append(
            kind="system",
            text=f"approved by {approval.approver or 'unknown'} (approval_id={approval.approval_id[:8]})",
        )

    # Dispatch to runtime — AdroidError bubbles up to caller
    if tool == "device.list":
        devices = state.runtime.list_devices(token=token)
        return {"devices": [d.model_dump(mode="json") for d in devices]}

    if tool == "device.observe":
        did = DeviceId.model_validate(args["device_id"])
        info = state.runtime.observe_device(token=token, device_id=did)
        return {"device": info.model_dump(mode="json")}

    if tool == "device.screenshot":
        did = DeviceId.model_validate(args["device_id"])
        shot = state.runtime.capture_screenshot(token=token, device_id=did)
        return {"screenshot": shot.model_dump(mode="json")}

    if tool == "app.list":
        did = DeviceId.model_validate(args["device_id"])
        apps = state.runtime.list_installed_apps(token=token, device_id=did)
        return {"apps": [a.model_dump(mode="json") for a in apps]}

    if tool == "app.launch":
        did = DeviceId.model_validate(args["device_id"])
        state.runtime.launch_app(
            token=token,
            device_id=did,
            package_name=args["package_name"],
        )
        return {"launched": True}

    # Should not reach here (tool_map check above would have 404'd)
    raise HTTPException(status_code=500, detail="unreachable")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    runtime: AdroidRuntime,
    broker: ApprovalBroker,
    interactive_gate: InteractiveGate,
    browser_session_id: str | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to the given runtime + broker + gate.

    The caller is responsible for constructing the runtime, broker, and
    gate — this function just wires them into HTTP endpoints.
    """
    terminal = TerminalStream()
    state = ServerState(
        runtime=runtime,
        broker=broker,
        interactive_gate=interactive_gate,
        terminal=terminal,
        browser_session_id=browser_session_id or f"browser-{secrets.token_hex(4)}",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        terminal.append(kind="system", text=f"runtime ready (issuer={runtime.issuer_id})")
        terminal.append(
            kind="system",
            text=f"open /ui in your browser — session_id={state.browser_session_id}",
        )
        yield

    app = FastAPI(
        title="Adroid Runtime",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.adroid = state
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # ------------------------------------------------------------------
    # Browser UI
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "issuer_id": runtime.issuer_id,
                "session_id": state.browser_session_id,
            },
        )

    @app.get("/ui", response_class=HTMLResponse)
    async def ui_alias(request: Request):
        return await index(request)

    @app.get("/api/pending")
    async def api_pending():
        return [p.to_dict() for p in state.broker.pending()]

    @app.get("/api/history")
    async def api_history(limit: int = 50):
        return [p.to_dict() for p in state.broker.history(limit=limit)]

    @app.get("/api/terminal")
    async def api_terminal(limit: int = 100):
        return state.terminal.history(limit=limit)

    @app.post("/api/approve/{approval_id}")
    async def api_approve(approval_id: str):
        ok = state.broker.resolve(
            approval_id,
            decision="approved",
            approver=state.browser_session_id,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="approval not found or already decided")
        return {"ok": True, "decision": "approved"}

    @app.post("/api/deny/{approval_id}")
    async def api_deny(approval_id: str):
        ok = state.broker.resolve(
            approval_id,
            decision="denied",
            approver=state.browser_session_id,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="approval not found or already decided")
        return {"ok": True, "decision": "denied"}

    @app.get("/api/tools")
    async def api_tools():
        return [t.model_dump(mode="json") for t in runtime.list_tools()]

    @app.get("/api/audit")
    async def api_audit(limit: int = 50):
        reader = runtime.audit_reader()
        events = reader.tail(n=limit)
        return [e.model_dump(mode="json") for e in events]

    @app.get("/blob/{blob_ref}")
    async def api_blob(blob_ref: str):
        data = runtime.get_blob(blob_ref)
        if data is None:
            raise HTTPException(status_code=404, detail="blob not found")
        return JSONResponse(
            content={"blob_ref": blob_ref, "size": len(data)},
            headers={"X-Blob-Ref": blob_ref},
        )

    # Blob bytes endpoint — returns raw bytes (used by browser <img>)
    @app.get("/blob/{blob_ref}/raw")
    async def api_blob_raw(blob_ref: str):
        from fastapi.responses import Response
        data = runtime.get_blob(blob_ref)
        if data is None:
            raise HTTPException(status_code=404, detail="blob not found")
        return Response(content=data, media_type="image/png")

    # ------------------------------------------------------------------
    # Agent HTTP API — what AI agents call
    # ------------------------------------------------------------------

    @app.post("/agent/call", response_model=AgentCallResponse)
    def agent_call(req: AgentCallRequest):
        try:
            token = CapabilityToken.model_validate(req.token)
        except ValidationError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"invalid token: {exc.errors()}",
            )

        approval_id: str | None = None
        try:
            # Snapshot the broker to detect if approval happened
            pending_before = {p.approval_id for p in state.broker.pending()}
            result = _dispatch_tool(
                state,
                token=token,
                tool=req.tool,
                args=req.args,
            )
            # Detect approval ID (whichever was added and resolved during the call)
            pending_after = {p.approval_id for p in state.broker.pending()}
            # Easier: look at broker history
            history = state.broker.history(limit=1)
            if history and history[0].decision == "approved":
                approval_id = history[0].approval_id

            state.terminal.append(
                kind="result",
                text=f"{req.tool} → ok",
            )
            return AgentCallResponse(
                ok=True,
                result=result,
                approval_id=approval_id,
            )
        except HTTPException:
            raise
        except AdroidError as exc:
            # Permission denied, bridge errors, etc. — return as structured
            # response so the agent sees the typed error code.
            state.terminal.append(
                kind="error",
                text=f"{req.tool} failed: {exc.code} — {exc}",
            )
            return AgentCallResponse(
                ok=False,
                error={
                    "code": exc.code,
                    "message": str(exc),
                    "details": exc.details,
                },
            )
        except Exception as exc:
            state.terminal.append(
                kind="error",
                text=f"{req.tool} raised {type(exc).__name__}: {exc}",
            )
            return AgentCallResponse(
                ok=False,
                error={
                    "code": "adroid.unexpected",
                    "message": str(exc),
                    "type": type(exc).__name__,
                },
            )

    # ------------------------------------------------------------------
    # WebSocket — live terminal + pending updates
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        queue = await state.terminal.subscribe()
        try:
            # Send history on connect
            await websocket.send_json({
                "type": "history",
                "lines": state.terminal.history(limit=100),
                "pending": [p.to_dict() for p in state.broker.pending()],
            })
            while True:
                # Poll for new terminal lines
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=1.0)
                    await websocket.send_json({"type": "terminal", "line": line})
                except asyncio.TimeoutError:
                    # No new line — send pending snapshot instead (cheap heartbeat)
                    await websocket.send_json({
                        "type": "pending",
                        "pending": [p.to_dict() for p in state.broker.pending()],
                    })
        except WebSocketDisconnect:
            pass
        finally:
            state.terminal.unsubscribe(queue)

    return app


# ---------------------------------------------------------------------------
# Programmatic server runner
# ---------------------------------------------------------------------------


def run_server(
    *,
    runtime: AdroidRuntime,
    broker: ApprovalBroker,
    interactive_gate: InteractiveGate,
    host: str = "0.0.0.0",
    port: int = 7654,
    browser_session_id: str | None = None,
) -> None:
    """Run the FastAPI app with uvicorn. Blocking."""
    import uvicorn

    app = create_app(
        runtime=runtime,
        broker=broker,
        interactive_gate=interactive_gate,
        browser_session_id=browser_session_id,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,  # too noisy for terminal-stream UX
    )
    server = uvicorn.Server(config)
    server.run()
