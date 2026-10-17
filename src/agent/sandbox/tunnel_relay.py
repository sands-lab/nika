"""Sandbox side of the NIKA host tunnel (stdlib only; runs inside the sandbox).

``python3 -u tunnel_relay.py PORT [PORT ...]`` listens on ``127.0.0.1:PORT``
for each port and forwards every accepted connection to the host over this
process's stdin/stdout, one JSON frame per line:

* stdout (to host): ``{"t": "ready"}``, ``{"t": "open", "id": N, "port": P}``,
  ``{"t": "data", "id": N, "b": "<base64>"}``, ``{"t": "eof", "id": N}``,
  ``{"t": "close", "id": N}``
* stdin (from host): ``{"t": "data", ...}``, ``{"t": "eof", ...}`` and
  ``{"t": "close", ...}``

``eof`` means one side finished sending but still reads, so a client can
half-close a connection and wait for the reply. The host closes a connection
once both sides sent ``eof``; either side sends ``close`` on errors.

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
        except OSError:
            self.drop(conn_id, notify=True)
            return
        # The client finished sending; it may still read the reply.
        if conn_id in self._writers:
            self.emit({"t": "eof", "id": conn_id})

    def drop(self, conn_id: int, *, notify: bool) -> None:
        writer = self._writers.pop(conn_id, None)
        if writer is None:
            return
        if notify:
            self.emit({"t": "close", "id": conn_id})
        writer.close()

    async def from_host(self, frame: dict) -> None:
        conn_id = int(frame.get("id", 0))
        writer = self._writers.get(conn_id)
        if writer is None:
            return
        kind = frame.get("t")
        try:
            if kind == "data":
                writer.write(base64.b64decode(frame.get("b", "")))
                await writer.drain()
            elif kind == "eof":
                writer.write_eof()
        except OSError:
            self.drop(conn_id, notify=True)
        if kind == "close":
            self.drop(conn_id, notify=False)


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
