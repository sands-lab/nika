#!/usr/bin/env python3
"""Static HTTP file server with a per-connection send-rate limit.

The limit uses kernel TCP pacing (``SO_MAX_PACING_RATE``), so a transfer
leaves the server as an even stream at the configured rate instead of in
bursts. Like a web server's ``limit_rate``, it caps each connection, not the
server total. A request may ask for a lower rate with ``?rate=`` (bit/s), as
``limit_rate $arg_rate`` does in nginx.
"""

from __future__ import annotations

import argparse
import functools
import socket
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

SO_MAX_PACING_RATE = getattr(socket, "SO_MAX_PACING_RATE", 47)


def bits_per_second(text: str) -> int:
    """Parse ``500k`` / ``5m`` / ``64000`` (bits per second)."""
    text = text.strip().lower()
    scale = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}.get(text[-1:], 1)
    value = text[:-1] if text[-1:] in "kmg" else text
    return int(float(value) * scale)


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    rate_bytes = 0

    def setup(self) -> None:
        super().setup()
        if self.rate_bytes:
            self.connection.setsockopt(
                socket.SOL_SOCKET, SO_MAX_PACING_RATE, self.rate_bytes
            )

    def do_GET(self) -> None:
        asked = parse_qs(urlsplit(self.path).query).get("rate")
        if asked:
            rate = bits_per_second(asked[0]) // 8
            if self.rate_bytes:
                rate = min(rate, self.rate_bytes)
            if rate > 0:
                self.connection.setsockopt(socket.SOL_SOCKET, SO_MAX_PACING_RATE, rate)
        super().do_GET()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="/var/www")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--rate", default="", help="per-connection limit, bit/s")
    args = parser.parse_args()
    Handler.rate_bytes = bits_per_second(args.rate) // 8 if args.rate else 0
    handler = functools.partial(Handler, directory=args.root)
    while True:
        try:
            ThreadingHTTPServer.allow_reuse_address = True
            ThreadingHTTPServer.daemon_threads = True
            ThreadingHTTPServer(("", args.port), handler).serve_forever()
        except OSError:
            time.sleep(5)


if __name__ == "__main__":
    main()
