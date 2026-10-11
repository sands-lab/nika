"""Host-side shim between Claude Code and a vLLM ``/v1/messages`` endpoint.

Claude Code treats any non-Anthropic ``ANTHROPIC_BASE_URL`` as first-party and
sends mid-conversation ``role: "system"`` turns from the first request on
(e.g. the "Today's date is ..." reminder). vLLM only accepts
``user``/``assistant`` roles there and rejects the request with HTTP 400,
which ends the agent run.

The shim folds each such turn into the neighbouring user turn as a
``<system-reminder>`` text block, which is how Claude Code itself renders
reminders for backends without mid-conversation system support, and streams
every request and response through unchanged otherwise.

The shim also owns authentication. Claude Code only knows a per-phase random
token (:func:`new_shim_token`); requests without it are rejected. The shim
replaces it with the real upstream key on the host, so the key never enters
the sandbox.
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent.sandbox.sbx.exec import SHIM_TOKEN_PREFIX
from nika.utils.net import pick_free_port

_AUTH_HEADERS = ("x-api-key", "authorization")

_HOP_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "accept-encoding",
        "content-encoding",
    }
)


def _system_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _append_blocks(message: dict[str, Any], blocks: list[dict[str, Any]]) -> dict:
    content = message.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    return {**message, "content": [*(content or []), *blocks]}


def fold_system_turns(body: dict[str, Any]) -> dict[str, Any]:
    """Return *body* with mid-conversation system turns merged into user turns."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not any(
        isinstance(m, dict) and m.get("role") == "system" for m in messages
    ):
        return body
    out: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            out.append(message)
            continue
        if message.get("role") == "system":
            text = _system_text(message.get("content")).strip()
            if text:
                block = {
                    "type": "text",
                    "text": f"<system-reminder>\n{text}\n</system-reminder>",
                }
                if out and out[-1].get("role") == "user":
                    out[-1] = _append_blocks(out[-1], [block])
                else:
                    pending.append(block)
            continue
        if pending and message.get("role") == "user":
            message = _append_blocks(message, pending)
            pending = []
        out.append(message)
    if pending:
        out.append({"role": "user", "content": pending})
    return {**body, "messages": out}


def new_shim_token() -> str:
    """Random per-phase credential that Claude Code presents to the shim."""
    return SHIM_TOKEN_PREFIX + secrets.token_urlsafe(24)


def _presented_token(request: Request) -> str:
    value = request.headers.get("x-api-key", "").strip()
    if value:
        return value
    auth = request.headers.get("authorization", "").strip()
    return auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""


def _upstream_headers(request: Request, api_key: str) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_HEADERS and k.lower() not in _AUTH_HEADERS
    }
    # Responses are streamed through raw; ask for them unencoded.
    headers["accept-encoding"] = "identity"
    if api_key:
        if request.headers.get("x-api-key"):
            headers["x-api-key"] = api_key
        if request.headers.get("authorization"):
            headers["authorization"] = f"Bearer {api_key}"
    return headers


def create_shim_app(upstream: str, *, token: str, api_key: str = "") -> Starlette:
    """Proxy to *upstream*; require *token* and send *api_key* upstream."""
    upstream = upstream.rstrip("/")
    client: httpx.AsyncClient | None = None

    async def proxy(request: Request) -> Response:
        nonlocal client
        if not secrets.compare_digest(_presented_token(request), token):
            return JSONResponse({"error": "invalid shim token"}, status_code=401)
        if client is None:
            client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=30.0))
        body = await request.body()
        if request.method == "POST" and request.url.path.endswith("/messages"):
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            if isinstance(data, dict):
                folded = fold_system_turns(data)
                if folded is not data:
                    body = json.dumps(folded).encode()
        upstream_request = client.build_request(
            request.method,
            upstream + request.url.path,
            params=request.query_params,
            headers=_upstream_headers(request, api_key),
            content=body,
        )
        response = await client.send(upstream_request, stream=True)
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers={
                k: v
                for k, v in response.headers.items()
                if k.lower() not in _HOP_HEADERS
            },
            background=BackgroundTask(response.aclose),
        )

    return Starlette(
        routes=[
            Route(
                "/{path:path}",
                proxy,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            )
        ]
    )


@contextmanager
def vllm_messages_shim(
    upstream: str, *, bind_host: str, token: str, api_key: str = ""
) -> Iterator[int]:
    """Serve the shim for *upstream* on *bind_host*; yield the listening port."""
    port = pick_free_port(bind_host)
    server = uvicorn.Server(
        uvicorn.Config(
            create_shim_app(upstream, token=token, api_key=api_key),
            host=bind_host,
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, name="nika-vllm-shim", daemon=True)
    thread.start()
    probe_host = "127.0.0.1" if bind_host == "0.0.0.0" else bind_host
    deadline = time.monotonic() + 10.0
    while True:
        try:
            with socket.create_connection((probe_host, port), timeout=0.2):
                break
        except OSError:
            if time.monotonic() > deadline or not thread.is_alive():
                server.should_exit = True
                raise RuntimeError(f"vLLM shim did not start on {bind_host}:{port}")
            time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
