"""ASGI middleware for session binding and phase gating."""

from __future__ import annotations

import hmac
import json
import time

from mcp.server.fastmcp import FastMCP

from nika.service.mcp_gateway.context import bind_session, reset_session
from nika.service.mcp_gateway.policy import is_server_allowed
from nika.service.mcp_gateway.session_registry import (
    GatewaySession,
    get_session,
    is_withheld,
)

SESSION_HEADER = "NIKA-Session-Id"
# Header names of the current NIKA frontend's agent harnesses. A benchmark
# engine's trial gateway accepts them next to ``NIKA-Session-Id``.
FRONTEND_SESSION_HEADER = "X-Session-Id"
SESSION_TOKEN_HEADER = "X-Session-Token"
_MCP_JSON = "application/json"

_empty_mcp = FastMCP("nika_phase_blocked")


def request_session_id(headers: dict[bytes, bytes]) -> str:
    """The session id an MCP or gateway request names, from either header."""
    for name in (SESSION_HEADER, FRONTEND_SESSION_HEADER):
        value = headers.get(name.lower().encode(), b"").decode().strip()
        if value:
            return value
    return ""


class PhaseGateMiddleware:
    """Bind session context and enforce phase policy for one MCP server mount."""

    def __init__(self, app, *, server_name: str, blocked_app):
        self.app = app
        self.blocked_app = blocked_app
        self.server_name = server_name

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        session_id = request_session_id(headers)

        if not session_id:
            await _send_json(
                send,
                status=400,
                payload={
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32000,
                        "message": f"Missing {SESSION_HEADER} header.",
                    },
                    "id": None,
                },
            )
            return

        entry = get_session(session_id)
        if entry is None and is_withheld(session_id):
            # A trial's own session ID names its task; agents get another one.
            await _send_json(
                send,
                status=403,
                payload={
                    "jsonrpc": "2.0",
                    "error": {"code": -32002, "message": "Unknown session."},
                    "id": None,
                },
            )
            return
        if entry is not None and entry.access_token is not None:
            presented = headers.get(SESSION_TOKEN_HEADER.lower().encode(), b"")
            if not hmac.compare_digest(presented.decode(), entry.access_token):
                await _send_json(
                    send,
                    status=401,
                    payload={
                        "jsonrpc": "2.0",
                        "error": {
                            "code": -32001,
                            "message": f"Missing or invalid {SESSION_TOKEN_HEADER}.",
                        },
                        "id": None,
                    },
                )
                return

        allowed = is_server_allowed(session_id, self.server_name)
        target_app = self.app if allowed else self.blocked_app

        # Tools address the lab and trial directory by the session's own ID.
        token = bind_session(entry.session_id if entry is not None else session_id)
        try:
            if entry is not None and entry.session_dir and scope["method"] == "POST":
                await _recorded(
                    target_app, scope, receive, send, entry, self.server_name, allowed
                )
            else:
                await target_app(scope, receive, send)
        finally:
            reset_session(token)


async def _recorded(
    app,
    scope,
    receive,
    send,
    entry: GatewaySession,
    server_name: str,
    allowed: bool,
) -> None:
    """Pass one request through unchanged; record it when it calls a tool."""
    from nika.service.mcp_gateway.tool_log import record_tool_call, tool_result

    messages = []
    body = b""
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":
            break
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    try:
        request = json.loads(body) if body else None
    except ValueError:
        request = None
    call = (
        request
        if isinstance(request, dict) and request.get("method") == "tools/call"
        else None
    )

    async def replay():
        if messages:
            return messages.pop(0)
        return await receive()

    if call is None:
        await app(scope, replay, send)
        return

    phase = entry.phase
    started = time.monotonic()
    content_type = ""
    response = b""

    async def observe(message):
        nonlocal content_type, response
        if message["type"] == "http.response.start":
            for name, value in message.get("headers") or []:
                if name.lower() == b"content-type":
                    content_type = value.decode("latin-1")
        elif message["type"] == "http.response.body":
            response += message.get("body", b"")
        await send(message)

    try:
        await app(scope, replay, observe)
    finally:
        params = call.get("params") if isinstance(call.get("params"), dict) else {}
        is_error, output = tool_result(response, content_type)
        try:
            record_tool_call(
                entry.session_dir or "",
                phase=phase,
                server=server_name,
                tool=str(params.get("name") or ""),
                arguments=params.get("arguments") or {},
                is_error=is_error,
                output=output,
                denied=None if allowed else "phase",
                call_id=call.get("id"),
                elapsed_ms=round((time.monotonic() - started) * 1000.0, 1),
            )
        except OSError:
            pass


async def _send_json(send, *, status: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", _MCP_JSON.encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
