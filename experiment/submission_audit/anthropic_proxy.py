"""Anthropic Messages proxy that makes Claude Code requests acceptable to vLLM.

Claude Code adds notices (for example "[Truncated: PARTIAL view ...]" after a
large Read) as messages with role ``system`` in the middle of a conversation.
vLLM's ``/v1/messages`` accepts only ``user`` and ``assistant`` there and
rejects the request. This proxy turns such messages into user text, merges
adjacent messages of the same role, and forwards everything else unchanged,
streaming the response back.

    uv run python -m experiment.submission_audit.anthropic_proxy \
        --upstream http://mcmgt01:18765 --port 18790

Point the auditing agent at it with ``ANTHROPIC_BASE_URL=http://127.0.0.1:18790``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

import aiohttp
from aiohttp import web

_HOP_HEADERS = {"host", "content-length", "transfer-encoding", "connection"}


def _blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def fix_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """System messages become user text; same-role neighbours are merged."""
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        blocks = _blocks(msg.get("content"))
        if role == "system":
            role = "user"
            blocks = [
                {**b, "text": f"[system notice] {b['text']}"}
                if b.get("type") == "text"
                else b
                for b in blocks
            ]
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({**msg, "role": role, "content": blocks})
    return out


async def handle(request: web.Request) -> web.StreamResponse:
    upstream: str = request.app["upstream"]
    body = await request.read()
    if request.path.endswith("/messages") and body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            payload["messages"] = fix_messages(payload["messages"])
            body = json.dumps(payload).encode()
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _HOP_HEADERS
    }
    session: aiohttp.ClientSession = request.app["session"]
    async with session.request(
        request.method, upstream + request.path_qs, data=body, headers=headers
    ) as resp:
        out = web.StreamResponse(
            status=resp.status,
            headers={
                k: v for k, v in resp.headers.items() if k.lower() not in _HOP_HEADERS
            },
        )
        await out.prepare(request)
        async for chunk in resp.content.iter_any():
            await out.write(chunk)
        await out.write_eof()
        return out


async def _start(app: web.Application) -> None:
    app["session"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))


async def _stop(app: web.Application) -> None:
    await app["session"].close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18790)
    args = parser.parse_args()
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app["upstream"] = args.upstream.rstrip("/")
    app.on_startup.append(_start)
    app.on_cleanup.append(_stop)
    app.router.add_route("*", "/{tail:.*}", handle)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
