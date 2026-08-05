"""Web layer — FastAPI server with session-based pairing + agent HTTP API."""

from adroid.web.server import (
    AgentCallRequest,
    AgentCallResponse,
    ApprovePairingRequest,
    ServerState,
    SessionRequest,
    SessionRequestResponse,
    SessionStatusResponse,
    TerminalStream,
    create_app,
    run_server,
)

__all__ = [
    "AgentCallRequest",
    "AgentCallResponse",
    "ApprovePairingRequest",
    "ServerState",
    "SessionRequest",
    "SessionRequestResponse",
    "SessionStatusResponse",
    "TerminalStream",
    "create_app",
    "run_server",
]
