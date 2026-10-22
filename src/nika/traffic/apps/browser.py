#!/usr/bin/env python3
"""Users fetching web pages and files over HTTP or HTTPS.

Each of ``--users`` independent users fetches one of the URLs at random, reads
the whole body and waits a random ``--pause``. Host names go through the
system resolver for every new connection, and HTTPS checks the server against
the system trust store. With ``--gzip`` the user asks for gzip and decodes the
body, so a damaged stream fails like it would in a package manager. With
``--keepalive`` a user keeps its connection to each server for a random
``--lifetime``, as browsers do. A failed or stalled transfer is dropped and
retried after a short random backoff.
"""

from __future__ import annotations

import argparse
import http.client
import random
import ssl
import threading
import time
import zlib
from urllib.parse import urlsplit

CHUNK = 64 * 1024


def seconds_range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


class User:
    def __init__(self, args: argparse.Namespace, seed: int) -> None:
        self.args = args
        self.rng = random.Random(seed)
        self.context = ssl.create_default_context()
        # (scheme, netloc) -> (connection, reuse deadline)
        self.connections: dict[tuple[str, str], tuple[object, float]] = {}

    def connection(self, scheme: str, netloc: str, host: str, port: int | None):
        key = (scheme, netloc)
        conn, until = self.connections.pop(key, (None, 0.0))
        if conn is not None and time.monotonic() < until:
            return conn, until
        if conn is not None:
            conn.close()
        if scheme == "https":
            conn = http.client.HTTPSConnection(
                host, port or 443, timeout=self.args.timeout, context=self.context
            )
        else:
            conn = http.client.HTTPConnection(
                host, port or 80, timeout=self.args.timeout
            )
        return conn, time.monotonic() + self.rng.uniform(
            *seconds_range(self.args.lifetime)
        )

    def fetch(self, url: str) -> None:
        parts = urlsplit(url)
        conn, until = self.connection(
            parts.scheme, parts.netloc, parts.hostname, parts.port
        )
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "*/*",
            "Accept-Encoding": "gzip" if self.args.gzip else "identity",
            "Connection": "keep-alive" if self.args.keepalive else "close",
        }
        try:
            conn.request("GET", path, headers=headers)
            response = conn.getresponse()
            if response.status != 200:
                raise OSError(f"HTTP {response.status}")
            encoded = (response.getheader("Content-Encoding") or "").lower()
            decoder = (
                zlib.decompressobj(16 + zlib.MAX_WBITS) if encoded == "gzip" else None
            )
            while True:
                chunk = response.read(CHUNK)
                if not chunk:
                    break
                if decoder is not None:
                    decoder.decompress(chunk)
            if decoder is not None and not decoder.eof:
                raise OSError("truncated gzip stream")
        except BaseException:
            conn.close()
            raise
        if self.args.keepalive and not response.will_close:
            self.connections[(parts.scheme, parts.netloc)] = (conn, until)
        else:
            conn.close()

    def run(self) -> None:
        pause = seconds_range(self.args.pause)
        # Users that start together do not stay in lockstep.
        time.sleep(self.rng.uniform(0, min(pause[1], 30.0)))
        while True:
            try:
                self.fetch(self.rng.choice(self.args.urls))
            except Exception:  # noqa: BLE001 - keep browsing after any failure
                time.sleep(self.rng.uniform(2, 8))
            time.sleep(self.rng.uniform(*pause))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--users", type=int, default=1)
    parser.add_argument("--pause", default="5-20", help="seconds between fetches")
    parser.add_argument("--gzip", action="store_true")
    parser.add_argument("--keepalive", action="store_true")
    parser.add_argument("--lifetime", default="60-240", help="connection reuse, s")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    users = [
        threading.Thread(target=User(args, args.seed * 1000 + index).run, daemon=True)
        for index in range(args.users)
    ]
    for user in users:
        user.start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
