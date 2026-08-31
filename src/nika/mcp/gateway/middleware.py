"""ASGI middleware for session binding and phase gating."""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP

from agent.protocols import DIAGNOSIS
from nika.mcp.gateway.access import decide_diagnosis_access
from nika.mcp.gateway.context import bind_session, reset_session
from nika.mcp.gateway.policy import is_server_allowed
from nika.mcp.gateway.session_registry import get_session

SESSION_HEADER = "NIKA-Session-Id"
_MCP_JSON = "application/json"

_empty_mcp = FastMCP("nika_phase_blocked")


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
        session_id = headers.get(b"nika-session-id", b"").decode().strip()

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
        target_app = (
            self.app
            if is_server_allowed(session_id, self.server_name)
            else self.blocked_app
        )

        # Streamable HTTP clients keep a GET SSE request open while issuing
        # JSON-RPC POSTs.  It has no call body to authorize; consuming it here
        # would wait forever and prevent tools/list from completing.
        if scope.get("method") != "POST":
            token = bind_session(session_id)
            try:
                await target_app(scope, receive, send)
            finally:
                reset_session(token)
            return

        body = await _read_body(receive)
        tool_name, arguments, request_id = _tool_call(body)
        if tool_name is not None:
            if entry is None:
                await _tool_denied(send, request_id, "unknown_session")
                return
            if entry.phase != DIAGNOSIS:
                allowed = (
                    self.server_name == "task_mcp_server" and tool_name == "submit"
                )
                reason = "" if allowed else "submission_network_access_denied"
            elif not is_server_allowed(session_id, self.server_name):
                allowed, reason = False, "diagnosis_server_not_allowed"
            else:
                decision = decide_diagnosis_access(
                    policy=entry.access_policy,
                    tool_name=tool_name,
                    arguments=arguments,
                    node_roles=entry.node_roles,
                )
                allowed, reason = decision.allowed, decision.reason
            if not allowed:
                await _tool_denied(send, request_id, reason)
                return

        token = bind_session(session_id)
        try:
            await target_app(scope, _replay_body(body), send)
        finally:
            reset_session(token)


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


async def _read_body(receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return b"".join(chunks)
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay_body(body: bytes):
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            # The inner Streamable HTTP app concurrently watches for a real
            # disconnect.  Do not fabricate one immediately after replaying a
            # POST body: FastMCP treats it as cancellation of initialization.
            await asyncio.Event().wait()
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _tool_call(body: bytes) -> tuple[str | None, dict, object]:
    try:
        payload = json.loads(body.decode() or "{}")
    except json.JSONDecodeError:
        return None, {}, None
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return None, {}, payload.get("id") if isinstance(payload, dict) else None
    params = payload.get("params") or {}
    if not isinstance(params, dict):
        return None, {}, payload.get("id")
    return (
        str(params.get("name") or ""),
        dict(params.get("arguments") or {}),
        payload.get("id"),
    )


async def _tool_denied(send, request_id: object, reason: str) -> None:
    await _send_json(
        send,
        # MCP application errors are JSON-RPC responses, not HTTP transport
        # failures.  Keeping HTTP 200 lets all supported MCP adapters surface
        # the denial to the agent instead of retrying the stream connection.
        status=200,
        payload={
            "jsonrpc": "2.0",
            "error": {"code": -32003, "message": f"NIKA access denied: {reason}"},
            "id": request_id,
        },
    )
