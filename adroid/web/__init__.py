"""Web layer — FastAPI server with permission UI + agent HTTP API + WebSocket."""

from adroid.web.server import (
    AgentCallRequest,
    AgentCallResponse,
    AsyncAgentCallResponse,
    AsyncResultResponse,
    ServerState,
    TerminalStream,
    create_app,
    run_server,
)

__all__ = [
    "AgentCallRequest",
    "AgentCallResponse",
    "AsyncAgentCallResponse",
    "AsyncResultResponse",
    "ServerState",
    "TerminalStream",
    "create_app",
    "run_server",
]
