#!/usr/bin/env python3
"""Clients of a public HTTP/1.1 service behind a NAT edge.

One process stands for the users of an access LAN:

- ``--sessions N`` persistent connections. Each one long-polls ``/events``
  (or, with ``--telemetry-every``, posts a small reading on that period) and
  is reopened after a random ``--lifetime``.
- ``--burst N`` page loads: every ``--burst-every`` seconds, N parallel
  short connections fetch one object each and close.
- ``--upload-kb N``: every ``--upload-every`` seconds, one N KB upload.

Failed requests are retried after a short random backoff.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import time

POLL_TIMEOUT_SEC = 45.0
REQUEST_TIMEOUT_SEC = 30.0
UPLOAD_TIMEOUT_SEC = 60.0


def seconds_range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


class Client:
    def __init__(self, args: argparse.Namespace) -> None:
        host, _, port = args.server.partition(":")
        self.host = host
        self.port = int(port or 80)
        self.args = args
        self.rng = random.Random(args.seed)

    def request(self, method: str, path: str, body: bytes, close: bool) -> bytes:
        head = (
            f"{method} {path} HTTP/1.1\r\nHost: {self.host}\r\n"
            "User-Agent: Mozilla/5.0\r\nAccept: */*\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: {'close' if close else 'keep-alive'}\r\n\r\n"
        )
        return head.encode() + body

    async def exchange(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        payload: bytes,
        timeout: float,
    ) -> None:
        async def run() -> None:
            writer.write(payload)
            await writer.drain()
            status = await reader.readline()
            if not status.startswith(b"HTTP/1.1 2"):
                raise OSError(f"bad response {status[:40]!r}")
            length = 0
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                name, _, value = line.decode("latin-1").partition(":")
                if name.strip().lower() == "content-length":
                    length = int(value.strip())
            if length:
                await reader.readexactly(length)

        await asyncio.wait_for(run(), timeout)

    async def connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), REQUEST_TIMEOUT_SEC
        )

    async def backoff(self) -> None:
        await asyncio.sleep(self.rng.uniform(2, 8))

    async def session(self) -> None:
        lifetime = seconds_range(self.args.lifetime)
        telemetry = self.args.telemetry_every
        await asyncio.sleep(self.rng.uniform(0, 10))
        while True:
            writer = None
            try:
                reader, writer = await self.connect()
                until = time.monotonic() + self.rng.uniform(*lifetime)
                while time.monotonic() < until:
                    if telemetry:
                        reading = b'{"t":%d}' % self.rng.randrange(1000)
                        await self.exchange(
                            reader,
                            writer,
                            self.request("POST", "/telemetry", reading, False),
                            REQUEST_TIMEOUT_SEC,
                        )
                        await asyncio.sleep(telemetry * self.rng.uniform(0.8, 1.2))
                    else:
                        await self.exchange(
                            reader,
                            writer,
                            self.request("GET", "/events", b"", False),
                            POLL_TIMEOUT_SEC,
                        )
            except (OSError, ValueError, asyncio.TimeoutError, EOFError):
                await self.backoff()
            finally:
                if writer is not None:
                    writer.close()

    async def fetch_once(self, kb: int) -> None:
        writer = None
        try:
            reader, writer = await self.connect()
            path = f"/static/{self.rng.randrange(1 << 20):x}.js?kb={kb}"
            await self.exchange(
                reader,
                writer,
                self.request("GET", path, b"", True),
                REQUEST_TIMEOUT_SEC,
            )
        except (OSError, ValueError, asyncio.TimeoutError, EOFError):
            pass
        finally:
            if writer is not None:
                writer.close()

    async def bursts(self) -> None:
        every = seconds_range(self.args.burst_every)
        size = seconds_range(self.args.burst_kb)
        while True:
            await asyncio.sleep(self.rng.uniform(*every))
            await asyncio.gather(
                *(
                    self.fetch_once(int(self.rng.uniform(*size)))
                    for _ in range(self.args.burst)
                )
            )

    async def uploads(self) -> None:
        every = seconds_range(self.args.upload_every)
        body = bytes(self.args.upload_kb * 1024)
        while True:
            await asyncio.sleep(self.rng.uniform(*every))
            writer = None
            try:
                reader, writer = await self.connect()
                await self.exchange(
                    reader,
                    writer,
                    self.request("POST", "/upload", body, True),
                    UPLOAD_TIMEOUT_SEC,
                )
            except (OSError, ValueError, asyncio.TimeoutError, EOFError):
                pass
            finally:
                if writer is not None:
                    writer.close()

    async def run(self) -> None:
        tasks = [self.session() for _ in range(self.args.sessions)]
        if self.args.burst:
            tasks.append(self.bursts())
        if self.args.upload_kb:
            tasks.append(self.uploads())
        await asyncio.gather(*tasks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("server", help="HOST[:PORT]")
    parser.add_argument("--sessions", type=int, default=0)
    parser.add_argument("--lifetime", default="60-240")
    parser.add_argument("--telemetry-every", type=float, default=0.0)
    parser.add_argument("--burst", type=int, default=0)
    parser.add_argument("--burst-every", default="8-15")
    parser.add_argument("--burst-kb", default="4-48")
    parser.add_argument("--upload-kb", type=int, default=0)
    parser.add_argument("--upload-every", default="60-180")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    while True:
        try:
            asyncio.run(Client(args).run())
        except Exception:  # noqa: BLE001 - restart the client after any crash
            time.sleep(5)


if __name__ == "__main__":
    main()
