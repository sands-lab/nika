"""Host-side model proxy between sandboxed agents and their LLM endpoint.

The proxy owns authentication. The agent only knows a random token
(:func:`new_proxy_token`); requests without it are rejected. The proxy replaces
it with the real upstream key on the host, so the key never enters the
sandbox.

It also adapts requests for self-hosted endpoints such as vLLM or Ollama, and
streams every request and response through unchanged otherwise:

* Claude Code treats any non-Anthropic ``ANTHROPIC_BASE_URL`` as first-party
  and sends mid-conversation ``role: "system"`` turns (e.g. the "Today's date
  is ..." reminder). vLLM only accepts ``user``/``assistant`` roles there and
  rejects the request with HTTP 400. :func:`fold_system_turns` folds each such
  turn into the neighbouring user turn as a ``<system-reminder>`` block, which
  is how Claude Code renders reminders for backends without mid-conversation
  system support.
* Codex sends each MCP server's tools as one Responses API ``namespace`` tool.
  Custom endpoints show the model only the namespace itself, as a function
  without parameters. :func:`flatten_namespace_tools` sends the tools as plain
  functions instead, and :func:`restore_tool_namespaces` adds the namespace
  back to the model's calls, which Codex needs to route them to its MCP tools
  (with ``non_prefixed_mcp_tool_names``).
"""

from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent.sandbox.constants import PROXY_TOKEN_PREFIX
from nika.utils.net import pick_free_port

_ANTHROPIC_API_URL = "https://api.anthropic.com"
_OPENAI_API_URL = "https://api.openai.com/v1"

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


def tool_namespaces(body: dict[str, Any]) -> dict[str, str]:
    """Map each tool inside a Responses ``namespace`` tool to its namespace."""
    namespaces: dict[str, str] = {}
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            for inner in tool.get("tools") or []:
                if isinstance(inner, dict) and inner.get("name"):
                    namespaces[inner["name"]] = str(tool.get("name", ""))
    return namespaces


def flatten_namespace_tools(body: dict[str, Any]) -> dict[str, Any]:
    """Return *body* with ``namespace`` tools replaced by the tools they hold.

    Function calls that Codex resends as history lose their ``namespace``
    too, so the endpoint only ever sees the bare names it was given.
    """
    tools = body.get("tools")
    if not isinstance(tools, list) or not any(
        isinstance(tool, dict) and tool.get("type") == "namespace" for tool in tools
    ):
        return body
    flat: list[Any] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("type") == "namespace":
            flat.extend(t for t in tool.get("tools") or [] if isinstance(t, dict))
        else:
            flat.append(tool)
    out = {**body, "tools": flat}
    if isinstance(body.get("input"), list):
        out["input"] = [
            {k: v for k, v in item.items() if k != "namespace"}
            if isinstance(item, dict) and item.get("type") == "function_call"
            else item
            for item in body["input"]
        ]
    return out


def restore_tool_namespaces(
    event: dict[str, Any], namespaces: Mapping[str, str]
) -> bool:
    """Add ``namespace`` to the function calls in a Responses stream event.

    Covers ``response.output_item.*`` (``item``) and events that carry the
    whole response (``response.output``). Returns whether *event* changed.
    """
    response = event.get("response")
    items = [event.get("item")]
    if isinstance(response, dict) and isinstance(response.get("output"), list):
        items.extend(response["output"])
    changed = False
    for item in items:
        if (
            isinstance(item, dict)
            and item.get("type") == "function_call"
            and not item.get("namespace")
            and item.get("name") in namespaces
        ):
            item["namespace"] = namespaces[item["name"]]
            changed = True
    return changed


def _json_object(raw: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _restore_event(block: bytes, namespaces: Mapping[str, str]) -> bytes:
    if b'"function_call"' not in block:
        return block
    lines = block.split(b"\n")
    for index, line in enumerate(lines):
        if line.startswith(b"data:"):
            data = _json_object(line[5:])
            if data is not None and restore_tool_namespaces(data, namespaces):
                lines[index] = b"data: " + json.dumps(data).encode()
    return b"\n".join(lines)


async def _restore_stream(
    chunks: AsyncIterator[bytes], namespaces: Mapping[str, str]
) -> AsyncIterator[bytes]:
    """Apply :func:`restore_tool_namespaces` to each server-sent event."""
    pending = b""
    async for chunk in chunks:
        pending += chunk
        *events, pending = pending.split(b"\n\n")
        for event in events:
            yield _restore_event(event, namespaces) + b"\n\n"
    if pending:
        yield _restore_event(pending, namespaces)


def new_proxy_token() -> str:
    """Random credential that a sandboxed agent presents to the proxy."""
    return PROXY_TOKEN_PREFIX + secrets.token_urlsafe(24)


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


def create_model_proxy_app(
    upstream: str, *, token: str, api_key: str = "", flatten_tools: bool = False
) -> Starlette:
    """Proxy to *upstream*; require *token* and send *api_key* upstream.

    *flatten_tools* applies :func:`flatten_namespace_tools` to Responses
    requests and :func:`restore_tool_namespaces` to their responses.
    """
    upstream = upstream.rstrip("/")
    client: httpx.AsyncClient | None = None

    async def proxy(request: Request) -> Response:
        nonlocal client
        if not secrets.compare_digest(_presented_token(request), token):
            return JSONResponse({"error": "invalid proxy token"}, status_code=401)
        if client is None:
            client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=30.0))
        body = await request.body()
        path = request.url.path
        namespaces: dict[str, str] = {}
        data = _json_object(body) if request.method == "POST" else None
        if data is not None and path.endswith("/messages"):
            folded = fold_system_turns(data)
            if folded is not data:
                body = json.dumps(folded).encode()
        elif data is not None and path.endswith("/responses") and flatten_tools:
            namespaces = tool_namespaces(data)
            if namespaces:
                body = json.dumps(flatten_namespace_tools(data)).encode()
        upstream_request = client.build_request(
            request.method,
            upstream + request.url.path,
            params=request.query_params,
            headers=_upstream_headers(request, api_key),
            content=body,
        )
        response = await client.send(upstream_request, stream=True)
        stream = response.aiter_raw()
        if namespaces:
            # Codex streams Responses calls as server-sent events.
            stream = _restore_stream(stream, namespaces)
        return StreamingResponse(
            stream,
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
def model_proxy(
    upstream: str,
    *,
    bind_host: str,
    token: str,
    api_key: str = "",
    flatten_tools: bool = False,
) -> Iterator[int]:
    """Serve the proxy for *upstream* on *bind_host*; yield the listening port."""
    port = pick_free_port(bind_host)
    server = uvicorn.Server(
        uvicorn.Config(
            create_model_proxy_app(
                upstream, token=token, api_key=api_key, flatten_tools=flatten_tools
            ),
            host=bind_host,
            port=port,
            log_level="warning",
        )
    )
    thread = threading.Thread(target=server.run, name="nika-model-proxy", daemon=True)
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
                raise RuntimeError(f"Model proxy did not start on {bind_host}:{port}")
            time.sleep(0.05)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def start_model_proxy(
    stack: ExitStack,
    *,
    env_file: Path,
    agent_type: str,
    provider: str,
    bind_host: str,
    agent_host: str,
) -> tuple[int, dict[str, str]]:
    """Start the model proxy for one sandbox session until *stack* closes.

    Returns the proxy port and the credential env that points the agent's
    client (Anthropic or OpenAI) at ``http://{agent_host}:{port}``.
    """
    from agent.registry import get_agent_spec
    from agent.sandbox.config import load_credential_sources
    from agent.utils.provider_env import (
        CUSTOM_UNAUTHENTICATED_API_KEY,
        ENV_ANTHROPIC_API_KEY,
        ENV_ANTHROPIC_AUTH_TOKEN,
        ENV_ANTHROPIC_BASE_URL,
        ENV_OPENAI_API_KEY,
        ENV_OPENAI_BASE_URL,
    )

    sources = load_credential_sources(
        env_file, provider=provider, agent_type=agent_type
    )
    anthropic = get_agent_spec(agent_type).credential_family == "anthropic"
    if anthropic:
        upstream = sources.get(ENV_ANTHROPIC_BASE_URL) or _ANTHROPIC_API_URL
        api_key = sources.get(ENV_ANTHROPIC_API_KEY) or sources.get(
            ENV_ANTHROPIC_AUTH_TOKEN, ""
        )
    else:
        upstream = sources.get(ENV_OPENAI_BASE_URL) or _OPENAI_API_URL
        api_key = sources.get(ENV_OPENAI_API_KEY, "")
    if api_key == CUSTOM_UNAUTHENTICATED_API_KEY:
        api_key = ""
    if not api_key and provider != "custom":
        raise RuntimeError(f"No API key found for provider {provider!r} in {env_file}.")
    token = new_proxy_token()
    port = stack.enter_context(
        model_proxy(
            upstream,
            bind_host=bind_host,
            token=token,
            api_key=api_key,
            # Codex enables bare MCP tool names only for custom providers.
            flatten_tools=provider == "custom",
        )
    )
    url = f"http://{agent_host}:{port}"
    if anthropic:
        env = {
            ENV_ANTHROPIC_BASE_URL: url,
            ENV_ANTHROPIC_API_KEY: token,
            ENV_ANTHROPIC_AUTH_TOKEN: token,
        }
    else:
        env = {ENV_OPENAI_BASE_URL: url, ENV_OPENAI_API_KEY: token}
    return port, env
