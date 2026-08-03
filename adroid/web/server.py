"""Web server + agent HTTP API for Adroid.

Architecture (v0.1.0 final):

    AI Agent (anywhere) ─POST /agent/session/request─→ LAPTOP (server)
                                                       │
    AI shares URL with human via chat                  │
                                                       │
    Human opens URL in browser                         │
    Human picks TTL + taps Approve                     │
                                                       │
    AI polls /agent/session/{pairing_id} ──────────────┤
                                                       │
    Server returns session token (full access, TTL-set)│
                                                       │
    AI calls /agent/call freely until TTL or disconnect│
                                                       │
                                                       ▼
                                                   ADB → HP

No per-action approval. One pairing → full access. Audit log records
every call; dashboard shows live terminal stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ValidationError

from adroid.contract.errors import AdroidError, PermissionDeniedError
from adroid.contract.types import (
    Capability,
    CapabilityToken,
    DeviceId,
    Scope,
)
from adroid.permissions.sessions import SessionManager
from adroid.permissions.tokens import TokenIssuer, validate_token_at
from adroid.runtime.core import AdroidRuntime

log = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"



# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class SessionRequest(BaseModel):
    """Body for POST /agent/session/request.

    The AI identifies itself with agent_id (free-form string). The
    bootstrap_token is a long-lived CapabilityToken the runtime prints at
    startup — it proves the AI is the one the user wants to pair (prevents
    randos from spamming pairing requests).
    """

    bootstrap_token: dict[str, Any]
    agent_id: str


class SessionRequestResponse(BaseModel):
    pairing_id: str
    pairing_url: str
    requested_at: str


class SessionStatusResponse(BaseModel):
    """Response for GET /agent/session/{pairing_id} — AI polls this."""

    status: str  # "pending" | "approved" | "denied" | "expired"
    session_token: dict[str, Any] | None = None  # present only when approved
    expires_at: str | None = None
    decided_at: str | None = None
    decided_by: str | None = None


class AgentCallRequest(BaseModel):
    token: dict[str, Any]
    tool: str
    args: dict[str, Any] = {}


class AgentCallResponse(BaseModel):
    ok: bool
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


class ApprovePairingRequest(BaseModel):
    ttl_seconds: int  # user-set; runtime validates range


# ---------------------------------------------------------------------------
# Terminal stream — live agent activity log for the browser
# ---------------------------------------------------------------------------


class TerminalStream:
    """Thread-safe ring buffer of agent activity lines."""

    def __init__(self, capacity: int = 500) -> None:
        self._lines: list[dict[str, Any]] = []
        self._capacity = capacity
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []

    def append(self, *, kind: str, text: str, **extra) -> None:
        line = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,  # "request" | "result" | "error" | "system" | "pairing" | "session"
            "text": text,
            **extra,
        }
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > self._capacity:
                self._lines = self._lines[-self._capacity:]
        for q in list(self._subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

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
# Server state
# ---------------------------------------------------------------------------


class ServerState:
    """Holds everything the web server needs at runtime."""

    def __init__(
        self,
        *,
        runtime: AdroidRuntime,
        session_manager: SessionManager,
        issuer: TokenIssuer,
        terminal: TerminalStream,
        browser_session_id: str,
        bootstrap_token: CapabilityToken,
    ):
        self.runtime = runtime
        self.sessions = session_manager
        self.issuer = issuer
        self.terminal = terminal
        self.browser_session_id = browser_session_id
        self.bootstrap_token = bootstrap_token


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------


def _dispatch_tool(
    state: ServerState,
    *,
    token: CapabilityToken,
    tool: str,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Execute a tool. Assumes the session token is valid + not revoked."""
    terminal = state.terminal

    tool_map = {
        "device.list": (Capability.DEVICE_LIST, Scope.READ),
        "device.observe": (Capability.DEVICE_OBSERVE, Scope.READ),
        "device.screenshot": (Capability.DEVICE_SCREENSHOT, Scope.READ),
        "app.list": (Capability.APP_LIST, Scope.READ),
        "app.launch": (Capability.APP_LAUNCH, Scope.ACT),
        # v0.1.0 input tools
        "input.tap": (Capability.INPUT_TAP, Scope.ACT),
        "input.tap_text": (Capability.INPUT_TAP_TEXT, Scope.ACT),
        "input.swipe": (Capability.INPUT_SWIPE, Scope.ACT),
        "input.text": (Capability.INPUT_TEXT, Scope.ACT),
        "input.key": (Capability.INPUT_KEY, Scope.ACT),
        # v0.1.0 shell + logs
        "shell.exec": (Capability.SHELL_EXEC, Scope.ACT),
        "logs.read": (Capability.LOGS_READ, Scope.READ),
        # v0.1.0 permission meta-tool
        "permission.request": (Capability.PERMISSION_REQUEST, Scope.READ),
        # v0.2.0 Semantic UI tools
        "ui.dump": (Capability.UI_DUMP, Scope.READ),
        "ui.find_elements": (Capability.UI_FIND_ELEMENTS, Scope.READ),
        "ui.tap_element": (Capability.UI_TAP_ELEMENT, Scope.ACT),
        "ui.wait_for": (Capability.UI_WAIT_FOR, Scope.READ),
    }

    if tool not in tool_map:
        raise HTTPException(
            status_code=404,
            detail=f"unknown tool: {tool}",
        )

    capability, required_scope = tool_map[tool]

    args_summary = json.dumps(args, default=str)
    terminal.append(
        kind="request",
        text=f"{tool}({args_summary})",
        subject=token.subject,
        capability=capability.value,
        scope=required_scope.value,
    )

    # Note: gate.authorize is called inside each runtime method (e.g.
    # runtime.list_devices, runtime.ui_tap_element). We do NOT call it
    # here to avoid double rate-limit consumption.

    if tool == "device.list":
        devices = state.runtime.list_devices(token=token)
        terminal.append(kind="result", text=f"{tool} → ok")
        return {"devices": [d.model_dump(mode="json") for d in devices]}

    if tool == "device.observe":
        did = DeviceId.model_validate(args["device_id"])
        info = state.runtime.observe_device(token=token, device_id=did)
        terminal.append(kind="result", text=f"{tool} → ok")
        return {"device": info.model_dump(mode="json")}

    if tool == "device.screenshot":
        did = DeviceId.model_validate(args["device_id"])
        shot = state.runtime.capture_screenshot(token=token, device_id=did)
        terminal.append(
            kind="result",
            text=f"{tool} → ok (blob={shot.blob_ref[:12]}…, {shot.width}x{shot.height})",
        )
        return {"screenshot": shot.model_dump(mode="json")}

    if tool == "app.list":
        did = DeviceId.model_validate(args["device_id"])
        apps = state.runtime.list_installed_apps(token=token, device_id=did)
        terminal.append(kind="result", text=f"{tool} → ok ({len(apps)} apps)")
        return {"apps": [a.model_dump(mode="json") for a in apps]}

    if tool == "app.launch":
        did = DeviceId.model_validate(args["device_id"])
        state.runtime.launch_app(
            token=token, device_id=did, package_name=args["package_name"]
        )
        terminal.append(kind="result", text=f"{tool} → ok")
        return {"launched": True}

    # v0.1.0 input tools
    if tool == "input.tap":
        did = DeviceId.model_validate(args["device_id"])
        state.runtime.tap(token=token, device_id=did, x=args["x"], y=args["y"])
        terminal.append(kind="result", text=f"{tool} → ok ({args['x']},{args['y']})")
        return {"tapped": True, "x": args["x"], "y": args["y"]}

    if tool == "input.tap_text":
        did = DeviceId.model_validate(args["device_id"])
        result = state.runtime.tap_text(
            token=token,
            device_id=did,
            text=args["text"],
            match=args.get("match", "first"),
            scroll_to_visible=args.get("scroll_to_visible", True),
        )
        terminal.append(
            kind="result",
            text=f"{tool} → ok (matched={result['matched_elements']}, scrolled={result['scrolled']})",
        )
        return result

    if tool == "input.swipe":
        did = DeviceId.model_validate(args["device_id"])
        state.runtime.swipe(
            token=token,
            device_id=did,
            x1=args["x1"], y1=args["y1"],
            x2=args["x2"], y2=args["y2"],
            duration_ms=args.get("duration_ms", 300),
        )
        terminal.append(kind="result", text=f"{tool} → ok")
        return {"swiped": True}

    if tool == "input.text":
        did = DeviceId.model_validate(args["device_id"])
        state.runtime.input_text(token=token, device_id=did, text=args["text"])
        terminal.append(kind="result", text=f"{tool} → ok ({len(args['text'])} chars)")
        return {"typed": True, "length": len(args["text"])}

    if tool == "input.key":
        did = DeviceId.model_validate(args["device_id"])
        state.runtime.press_key(token=token, device_id=did, keycode=args["keycode"])
        terminal.append(kind="result", text=f"{tool} → ok ({args['keycode']})")
        return {"pressed": True, "keycode": args["keycode"]}

    # v0.1.0 shell + logs
    if tool == "shell.exec":
        did = DeviceId.model_validate(args["device_id"])
        result = state.runtime.run_shell(
            token=token, device_id=did, command=args["command"],
        )
        terminal.append(
            kind="result",
            text=f"{tool} → ok (rc={result['returncode']}, {result['duration_ms']}ms, {len(result['stdout'])}B stdout)",
        )
        return result

    if tool == "logs.read":
        did = DeviceId.model_validate(args["device_id"])
        result = state.runtime.read_logs(
            token=token, device_id=did,
            lines=args.get("lines", 100),
            filter=args.get("filter"),
        )
        terminal.append(
            kind="result",
            text=f"{tool} → ok ({len(result['lines'])} lines, {result['total_bytes']}B)",
        )
        return result

    # v0.1.0 permission meta-tool
    if tool == "permission.request":
        result = state.runtime.request_permission(
            token=token,
            scope=args["scope"],
            reason=args.get("reason"),
        )
        terminal.append(
            kind="result",
            text=f"{tool} → ok (scope={args['scope']}, status={result['status']})",
        )
        return result

    # v0.2.0 Semantic UI tools
    if tool == "ui.dump":
        did = DeviceId.model_validate(args["device_id"])
        result = state.runtime.ui_dump(token=token, device_id=did)
        ws = result["world_state"]
        terminal.append(
            kind="result",
            text=f"{tool} → ok (revision={ws['revision']}, nodes={len(ws['nodes'])}, fg={ws.get('foreground_package','?')})",
        )
        return result

    if tool == "ui.find_elements":
        did = DeviceId.model_validate(args["device_id"])
        result = state.runtime.ui_find_elements(
            token=token, device_id=did, selector=args["selector"],
        )
        terminal.append(
            kind="result",
            text=f"{tool} → ok (matched={len(result['elements'])})",
        )
        return result

    if tool == "ui.tap_element":
        did = DeviceId.model_validate(args["device_id"])
        result = state.runtime.ui_tap_element(
            token=token, device_id=did, selector=args["selector"],
        )
        if result["tapped"]:
            node = result.get("tapped_node", {})
            terminal.append(
                kind="result",
                text=f"{tool} → ok (matched={result['matched_nodes']}, tapped text={node.get('text','?')!r} at {result.get('tap_coordinates')})",
            )
        else:
            terminal.append(
                kind="result",
                text=f"{tool} → no match (matched=0, not tapped)",
            )
        return result

    if tool == "ui.wait_for":
        did = DeviceId.model_validate(args["device_id"])
        result = state.runtime.ui_wait_for(
            token=token, device_id=did, condition=args["condition"],
        )
        terminal.append(
            kind="result",
            text=f"{tool} → {result['status']} (polls={result['polls']}, {result['duration_seconds']:.1f}s)",
        )
        return result

    raise HTTPException(status_code=500, detail="unreachable")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    runtime: AdroidRuntime,
    session_manager: SessionManager,
    issuer: TokenIssuer,
    bootstrap_token: CapabilityToken,
    browser_session_id: str | None = None,
) -> FastAPI:
    """Build the FastAPI app."""
    terminal = TerminalStream()
    state = ServerState(
        runtime=runtime,
        session_manager=session_manager,
        issuer=issuer,
        terminal=terminal,
        browser_session_id=browser_session_id or f"browser-{secrets.token_hex(4)}",
        bootstrap_token=bootstrap_token,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        terminal.append(kind="system", text=f"runtime ready (issuer={runtime.issuer_id})")
        terminal.append(
            kind="system",
            text=f"open /ui in your browser — session_id={state.browser_session_id}",
        )
        # Background cleanup of expired sessions every 60s
        async def cleanup_loop():
            while True:
                await asyncio.sleep(60)
                removed = session_manager.cleanup_expired()
                if removed:
                    terminal.append(
                        kind="system",
                        text=f"cleaned up {removed} expired session(s)",
                    )

        task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="Adroid Runtime", version="0.1.0", lifespan=lifespan)
    app.state.adroid = state

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

    @app.get("/api/state")
    async def api_state():
        """Single endpoint the dashboard polls for everything."""
        return {
            "pairings": [p.to_dict() for p in session_manager.list_pending_pairings()],
            "sessions": [s.to_dict() for s in session_manager.list_sessions()],
            "terminal": state.terminal.history(limit=100),
            "issuer_id": runtime.issuer_id,
            "browser_session_id": state.browser_session_id,
        }

    @app.get("/api/audit")
    async def api_audit(limit: int = 50):
        reader = runtime.audit_reader()
        events = reader.tail(n=limit)
        return [e.model_dump(mode="json") for e in events]

    @app.get("/api/tools")
    async def api_tools():
        return [t.model_dump(mode="json") for t in runtime.list_tools()]

    @app.post("/api/pair/{pairing_id}/approve")
    async def api_approve_pairing(pairing_id: str, req: ApprovePairingRequest):
        try:
            p = session_manager.approve_pairing(
                pairing_id=pairing_id,
                ttl_seconds=req.ttl_seconds,
                approver=state.browser_session_id,
            )
        except AdroidError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})

        state.terminal.append(
            kind="pairing",
            text=f"approved pairing {pairing_id[:8]} for agent={p.agent_id} ttl={req.ttl_seconds}s",
        )
        return {"ok": True, "pairing": p.to_dict()}

    @app.post("/api/pair/{pairing_id}/deny")
    async def api_deny_pairing(pairing_id: str):
        try:
            p = session_manager.deny_pairing(
                pairing_id=pairing_id,
                approver=state.browser_session_id,
            )
        except AdroidError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})

        state.terminal.append(
            kind="pairing",
            text=f"denied pairing {pairing_id[:8]} for agent={p.agent_id}",
        )
        return {"ok": True, "pairing": p.to_dict()}

    @app.post("/api/session/{session_id}/disconnect")
    async def api_disconnect_session(session_id: str):
        ok = session_manager.disconnect_session(session_id)
        if not ok:
            raise HTTPException(status_code=404, detail="session not found or already revoked")
        state.terminal.append(
            kind="session",
            text=f"disconnected session {session_id[:8]}",
        )
        return {"ok": True}

    @app.get("/blob/{blob_ref}/raw")
    async def api_blob_raw(blob_ref: str):
        data = runtime.get_blob(blob_ref)
        if data is None:
            raise HTTPException(status_code=404, detail="blob not found")
        return Response(content=data, media_type="image/png")

    # ------------------------------------------------------------------
    # Agent HTTP API — pairing
    # ------------------------------------------------------------------

    @app.post("/agent/session/request", response_model=SessionRequestResponse)
    def agent_session_request(req: SessionRequest, request: Request):
        """AI calls this to start a pairing. Returns pairing_id + URL.

        The bootstrap_token must match the one the runtime printed at
        startup. This prevents randos from spamming pairing requests.
        """
        # Validate bootstrap token
        try:
            bt = CapabilityToken.model_validate(req.bootstrap_token)
            validate_token_at(
                bt,
                runtime._token_public,  # noqa: SLF001
                expected_issuer=runtime.issuer_id,
            )
        except (ValidationError, PermissionDeniedError) as exc:
            raise HTTPException(
                status_code=401,
                detail=f"invalid bootstrap_token: {exc}",
            )

        # Bootstrap token must be the exact one the runtime printed
        if bt.token_id != state.bootstrap_token.token_id:
            raise HTTPException(
                status_code=401,
                detail="bootstrap_token mismatch — use the one the runtime printed at startup",
            )

        try:
            p = session_manager.request_pairing(agent_id=req.agent_id)
        except AdroidError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})

        base_url = str(request.base_url).rstrip("/")
        pairing_url = f"{base_url}/ui#pairing-{p.pairing_id}"

        state.terminal.append(
            kind="pairing",
            text=f"pairing requested by agent={req.agent_id} (id={p.pairing_id[:8]})",
        )

        return SessionRequestResponse(
            pairing_id=p.pairing_id,
            pairing_url=pairing_url,
            requested_at=p.requested_at.isoformat(),
        )

    @app.get("/agent/session/{pairing_id}", response_model=SessionStatusResponse)
    def agent_session_status(pairing_id: str):
        """AI polls this until status != 'pending'. When approved, the
        response includes the session_token (full access, TTL-set)."""
        p = session_manager.get_pairing(pairing_id)
        if p is None:
            raise HTTPException(status_code=404, detail=f"unknown pairing_id: {pairing_id}")

        if p.status == "approved" and p.session_token_json:
            token_obj = json.loads(p.session_token_json)
            return SessionStatusResponse(
                status="approved",
                session_token=token_obj,
                expires_at=token_obj.get("expires_at"),
                decided_at=p.decided_at.isoformat() if p.decided_at else None,
                decided_by=p.decided_by,
            )
        return SessionStatusResponse(
            status=p.status,
            decided_at=p.decided_at.isoformat() if p.decided_at else None,
            decided_by=p.decided_by,
        )

    # ------------------------------------------------------------------
    # Agent HTTP API — tool calls (no per-action approval!)
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

        # Check revocation FIRST — even before standard gate validation
        if session_manager.is_revoked(token.token_id):
            state.terminal.append(
                kind="error",
                text=f"{req.tool} rejected: session revoked",
            )
            return AgentCallResponse(
                ok=False,
                error={
                    "code": "adroid.session.revoked",
                    "message": "session has been disconnected by the user",
                },
            )

        try:
            result = _dispatch_tool(state, token=token, tool=req.tool, args=req.args)
            return AgentCallResponse(ok=True, result=result)
        except HTTPException:
            raise
        except AdroidError as exc:
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
    # WebSocket — live terminal + state updates
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def ws_endpoint(websocket: WebSocket):
        await websocket.accept()
        queue = await state.terminal.subscribe()
        try:
            await websocket.send_json({
                "type": "init",
                "state": {
                    "pairings": [p.to_dict() for p in session_manager.list_pending_pairings()],
                    "sessions": [s.to_dict() for s in session_manager.list_sessions()],
                    "terminal": state.terminal.history(limit=100),
                },
            })
            while True:
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=2.0)
                    await websocket.send_json({"type": "terminal", "line": line})
                except asyncio.TimeoutError:
                    # Heartbeat with current state snapshot
                    await websocket.send_json({
                        "type": "state",
                        "state": {
                            "pairings": [p.to_dict() for p in session_manager.list_pending_pairings()],
                            "sessions": [s.to_dict() for s in session_manager.list_sessions()],
                        },
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
    session_manager: SessionManager,
    issuer: TokenIssuer,
    bootstrap_token: CapabilityToken,
    host: str = "0.0.0.0",
    port: int = 7654,
    browser_session_id: str | None = None,
) -> None:
    """Run the FastAPI app with uvicorn. Blocking."""
    import uvicorn

    app = create_app(
        runtime=runtime,
        session_manager=session_manager,
        issuer=issuer,
        bootstrap_token=bootstrap_token,
        browser_session_id=browser_session_id,
    )
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
    )
    server = uvicorn.Server(config)
    server.run()
