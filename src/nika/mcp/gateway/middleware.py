"""ASGI middleware for session binding, phase gating, and tool-output bounds."""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP

from agent.protocols import DIAGNOSIS
from nika.mcp.fastmcp_settings import ensure_fastmcp_settings_ready
from nika.mcp.gateway.access import decide_diagnosis_access
from nika.mcp.gateway.context import bind_session, reset_session
from nika.mcp.gateway.policy import is_server_allowed
from nika.mcp.gateway.session_registry import (
    bind_transport_session,
    get_session,
    transport_session_matches,
)
from nika.mcp.tool_output import (
    jsonrpc_method,
    rewrite_http_body,
    tool_output_max_chars,
)

SESSION_HEADER = "NIKA-Session-Id"
_MCP_JSON = "application/json"

ensure_fastmcp_settings_ready()
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

        transport_id = headers.get(b"mcp-session-id", b"").decode().strip()
        if transport_id and not transport_session_matches(transport_id, session_id):
            await _send_json(
                send,
                status=403,
                payload={
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32003,
                        "message": (
                            f"{SESSION_HEADER} does not match the session that "
                            "initialized this MCP connection."
                        ),
                    },
                    "id": None,
                },
            )
            return
        if not transport_id:
            send = _TransportBindingSend(send, session_id)

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
        method = jsonrpc_method(body)
        if method == "tools/call":
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

        rewrite = method in {"tools/call", "tools/list"}
        outbound = (
            _RewritingSend(send, bound_call=(method == "tools/call"))
            if rewrite
            else send
        )

        token = bind_session(session_id)
        try:
            await target_app(scope, _replay_body(body), outbound)
        finally:
            reset_session(token)


class _TransportBindingSend:
    """Record the MCP transport session id issued to *session_id*."""

    def __init__(self, send, session_id: str):
        self._send = send
        self._session_id = session_id

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            transport_id = _header_value(
                list(message.get("headers") or []), b"mcp-session-id"
            ).strip()
            if transport_id:
                bind_transport_session(transport_id, self._session_id)
        await self._send(message)


class _RewritingSend:
    """Buffer one HTTP response and rewrite MCP tool list/call payloads."""

    def __init__(self, send, *, bound_call: bool):
        self._send = send
        self._bound_call = bound_call
        self._status = 200
        self._headers: list[tuple[bytes, bytes]] = []
        self._chunks: list[bytes] = []

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self._status = int(message.get("status") or 200)
            self._headers = list(message.get("headers") or [])
            return
        if message["type"] != "http.response.body":
            await self._send(message)
            return
        self._chunks.append(message.get("body") or b"")
        if message.get("more_body"):
            return

        body = b"".join(self._chunks)
        content_type = _header_value(self._headers, b"content-type") or _MCP_JSON
        new_body = rewrite_http_body(
            body,
            content_type=content_type,
            max_chars=tool_output_max_chars(),
            bound_call=self._bound_call,
        )
        headers = [
            (name, value)
            for name, value in self._headers
            if name.lower() not in {b"content-length", b"transfer-encoding"}
        ]
        headers.append((b"content-length", str(len(new_body)).encode()))
        await self._send(
            {
                "type": "http.response.start",
                "status": self._status,
                "headers": headers,
            }
        )
        await self._send({"type": "http.response.body", "body": new_body})


def _header_value(headers: list[tuple[bytes, bytes]], name: bytes) -> str:
    needle = name.lower()
    for key, value in headers:
        if key.lower() == needle:
            return value.decode("latin-1")
    return ""


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
