"""Sandbox side of the NIKA host tunnel (stdlib only; runs inside the sandbox).

``python3 -u tunnel_relay.py PORT [PORT ...]`` listens on ``127.0.0.1:PORT``
for each port and forwards every accepted connection to the host over this
process's stdin/stdout, one JSON frame per line:

* stdout (to host): ``{"t": "ready"}``, ``{"t": "open", "id": N, "port": P}``,
  ``{"t": "data", "id": N, "b": "<base64>"}``, ``{"t": "close", "id": N}``
* stdin (from host): ``{"t": "data", ...}`` and ``{"t": "close", ...}``

The host decides which service each port reaches (see
:mod:`agent.sandbox.tunnel`); the sandbox needs no network route to the host.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys

_CHUNK = 64 * 1024


class Relay:
    def __init__(self) -> None:
        self._writers: dict[int, asyncio.StreamWriter] = {}
        self._next_id = 0

    def emit(self, frame: dict) -> None:
        sys.stdout.write(json.dumps(frame, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    async def handle(
        self, port: int, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._next_id += 1
        conn_id = self._next_id
        self._writers[conn_id] = writer
        self.emit({"t": "open", "id": conn_id, "port": port})
        try:
            while chunk := await reader.read(_CHUNK):
                self.emit(
                    {"t": "data", "id": conn_id, "b": base64.b64encode(chunk).decode()}
                )
        except ConnectionError:
            pass
        finally:
            if self._writers.pop(conn_id, None) is not None:
                self.emit({"t": "close", "id": conn_id})
                writer.close()

    async def from_host(self, frame: dict) -> None:
        writer = self._writers.get(int(frame.get("id", 0)))
        if writer is None:
            return
        if frame.get("t") == "data":
            writer.write(base64.b64decode(frame.get("b", "")))
            try:
                await writer.drain()
            except ConnectionError:
                pass
        elif frame.get("t") == "close":
            self._writers.pop(int(frame["id"]), None)
            writer.close()


async def main(ports: list[int]) -> None:
    relay = Relay()
    for port in ports:
        await asyncio.start_server(
            lambda r, w, p=port: relay.handle(p, r, w), "127.0.0.1", port
        )
    relay.emit({"t": "ready"})

    loop = asyncio.get_running_loop()
    stdin = asyncio.StreamReader(limit=16 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
    while line := await stdin.readline():
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if isinstance(frame, dict):
            await relay.from_host(frame)


if __name__ == "__main__":
    asyncio.run(main([int(arg) for arg in sys.argv[1:]]))
