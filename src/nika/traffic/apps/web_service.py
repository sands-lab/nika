#!/usr/bin/env python3
"""Public HTTP/1.1 web and push service.

Routes:

- ``GET /events``: long poll. The reply comes after a random 15-30 s hold, the
  way push and messaging apps keep a persistent connection open.
- ``GET /static/<name>?kb=N``: an N KB object (default 16).
- ``POST /upload`` and ``POST /telemetry``: read the body, reply ``204``.

Connections stay open (keep-alive) unless the client sends
``Connection: close``.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time
from urllib.parse import parse_qs, urlsplit

IDLE_SEC = 300.0
MAX_BODY = 64 * 1024 * 1024


async def read_request(reader: asyncio.StreamReader) -> tuple[str, str, dict] | None:
    line = await asyncio.wait_for(reader.readline(), IDLE_SEC)
    if not line:
        return None
    method, target, _version = line.decode("latin-1").split(" ", 2)
    headers: dict[str, str] = {}
    while True:
        raw = await asyncio.wait_for(reader.readline(), IDLE_SEC)
        if raw in (b"\r\n", b"\n", b""):
            break
        name, _, value = raw.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    length = min(int(headers.get("content-length") or 0), MAX_BODY)
    while length > 0:
        chunk = await asyncio.wait_for(reader.read(min(length, 65536)), IDLE_SEC)
        if not chunk:
            return None
        length -= len(chunk)
    return method, target, headers


def response(status: str, body: bytes, close: bool, kind: str) -> bytes:
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {kind}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        f"Connection: {'close' if close else 'keep-alive'}\r\n\r\n"
    )
    return head.encode() + body


async def handle(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, rng: random.Random
) -> None:
    try:
        while True:
            request = await read_request(reader)
            if request is None:
                break
            method, target, headers = request
            close = headers.get("connection", "").lower() == "close"
            url = urlsplit(target)
            if method == "GET" and url.path == "/events":
                await asyncio.sleep(rng.uniform(15, 30))
                reply = response("200 OK", b'{"events":[]}', close, "application/json")
            elif method == "GET" and url.path.startswith("/static/"):
                kb = int((parse_qs(url.query).get("kb") or ["16"])[0])
                body = bytes(min(max(kb, 0), 4096) * 1024)
                reply = response("200 OK", body, close, "application/octet-stream")
            elif method == "POST":
                reply = response("204 No Content", b"", close, "text/plain")
            else:
                reply = response("404 Not Found", b"", close, "text/plain")
            writer.write(reply)
            await writer.drain()
            if close:
                break
    except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def serve(host: str, port: int, seed: int) -> None:
    rng = random.Random(seed)
    server = await asyncio.start_server(
        lambda r, w: handle(r, w, rng), host or None, port, backlog=4096
    )
    async with server:
        await server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="")
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    while True:
        try:
            asyncio.run(serve(args.bind, args.port, args.seed))
        except OSError:
            time.sleep(5)


if __name__ == "__main__":
    main()
