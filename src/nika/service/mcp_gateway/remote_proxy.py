"""ASGI reverse proxy to a per-session remote MCP HTTP upstream."""

from __future__ import annotations

import logging
from typing import Iterable

import httpx

from nika.service.mcp_gateway.context import get_bound_session_id
from nika.service.mcp_gateway.session_registry import get_session

logger = logging.getLogger(__name__)

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _filter_request_headers(headers: Iterable[tuple[bytes, bytes]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = raw_name.decode("latin-1")
        if name.lower() in _HOP_BY_HOP:
            continue
        out[name] = raw_value.decode("latin-1")
    return out


def _upstream_url(base: str, path: str, query: str) -> str:
    """Join upstream base with the mount-relative path (preserve ``/mcp``)."""
    if not path.startswith("/"):
        path = "/" + path
    target = base.rstrip("/") + path
    if query:
        return f"{target}?{query}"
    return target


def _upstream_host_header(base: str) -> str:
    """FastMCP rejects Docker-bridge IPs as Host; spoof loopback for the upstream."""
    from urllib.parse import urlparse

    parsed = urlparse(base)
    port = parsed.port
    # Always present as loopback so Starlette/FastMCP host checks pass.
    if port:
        return f"127.0.0.1:{port}"
    return "127.0.0.1"


class RemoteMcpProxy:
    """Forward streamable-HTTP MCP traffic to the session's remote upstream."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"WebSocket not supported for remote MCP proxy",
                }
            )
            return

        session_id = get_bound_session_id()
        entry = get_session(session_id) if session_id else None
        upstream = (
            (entry.remote_upstreams or {}).get(self.server_name) if entry else None
        )
        if not upstream:
            body = (
                b'{"jsonrpc":"2.0","error":{"code":-32000,'
                b'"message":"No remote MCP upstream for this session."},"id":null}'
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        # In-node server always exposes streamable HTTP at ``/mcp``. Ignore any
        # gateway mount prefix that may still be present in scope["path"].
        path = "/mcp"
        query = scope.get("query_string", b"").decode("latin-1")
        target = _upstream_url(upstream, path, query)
        method = scope.get("method", "GET").upper()
        req_headers = _filter_request_headers(scope.get("headers") or [])
        req_headers["host"] = _upstream_host_header(upstream)

        body_chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                continue
            body_chunks.append(message.get("body", b"") or b"")
            if not message.get("more_body"):
                break
        request_body = b"".join(body_chunks)

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    method,
                    target,
                    headers=req_headers,
                    content=request_body if request_body else None,
                ) as response:
                    resp_headers = [
                        (k.encode("latin-1"), v.encode("latin-1"))
                        for k, v in response.headers.multi_items()
                        if k.lower() not in _HOP_BY_HOP
                    ]
                    await send(
                        {
                            "type": "http.response.start",
                            "status": response.status_code,
                            "headers": resp_headers,
                        }
                    )
                    async for chunk in response.aiter_raw():
                        if chunk:
                            await send(
                                {
                                    "type": "http.response.body",
                                    "body": chunk,
                                    "more_body": True,
                                }
                            )
                    await send(
                        {"type": "http.response.body", "body": b"", "more_body": False}
                    )
        except httpx.HTTPError as exc:
            logger.warning(
                "Remote MCP proxy error for %s session=%s: %s",
                self.server_name,
                session_id,
                exc,
            )
            err = (
                b'{"jsonrpc":"2.0","error":{"code":-32000,'
                b'"message":"Remote MCP upstream request failed."},"id":null}'
            )
            await send(
                {
                    "type": "http.response.start",
                    "status": 502,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(err)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": err})
