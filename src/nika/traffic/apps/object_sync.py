#!/usr/bin/env python3
"""Object backup client and object store over HTTP.

``store``: an object store. It accepts ``PUT /objects/<id>`` uploads, reads
each body, and answers ``201 Created``.

``backup``: a backup agent. It uploads objects to the ``--store`` servers,
each picked in proportion to its weight. Uploads start at random times that
average ``--rate-kbps`` of data; object sizes follow the ``--object-mix``
(``web``, ``analytics`` or ``batch``), capped at ``--max-object-kb``. With
``--upload-kbps`` each upload is sent no faster than that rate. With
``--cycle`` the upload rate rises and falls over that period, by the relative
``--cycle-depth``.
"""

from __future__ import annotations

import argparse
import bisect
import math
import os
import random
import socket
import threading
import time

CHUNK = 64 * 1024
CONNECT_TIMEOUT = 5.0
# Uploads one agent keeps open at once; later ones wait for the next round.
MAX_ACTIVE = 64
SO_MAX_PACING_RATE = getattr(socket, "SO_MAX_PACING_RATE", 47)

# Object sizes: (bytes, cumulative share).
MIXES = {
    "web": (
        (0, 0.0), (10_000, 0.15), (20_000, 0.20), (30_000, 0.30),
        (50_000, 0.40), (80_000, 0.53), (200_000, 0.60), (1_000_000, 0.70),
        (2_000_000, 0.80), (5_000_000, 0.90), (10_000_000, 0.97),
        (30_000_000, 1.0),
    ),
    "analytics": (
        (0, 0.0), (180, 0.1), (216, 0.2), (560, 0.3), (900, 0.4),
        (1_100, 0.5), (1_870, 0.6), (3_160, 0.7), (10_000, 0.8),
        (400_000, 0.9), (3_160_000, 0.95), (100_000_000, 0.98),
        (1_000_000_000, 1.0),
    ),
    "batch": (
        (0, 0.0), (100, 0.01), (200, 0.02), (300, 0.05), (350, 0.15),
        (400, 0.20), (500, 0.30), (600, 0.40), (700, 0.50), (1_000, 0.60),
        (2_000, 0.67), (7_000, 0.70), (30_000, 0.72), (50_000, 0.82),
        (80_000, 0.87), (120_000, 0.90), (300_000, 0.95), (1_000_000, 0.975),
        (2_000_000, 0.99), (10_000_000, 1.0),
    ),
}  # fmt: skip


class ObjectSizes:
    """Draws object sizes from a size mix, capped at ``cap`` bytes."""

    def __init__(self, name: str, cap: int) -> None:
        points = MIXES[name]
        self.sizes = [float(size) for size, _ in points]
        self.shares = [share for _, share in points]
        self.cap = cap

    def draw(self, rng: random.Random) -> int:
        u = rng.random()
        i = max(1, bisect.bisect_left(self.shares, u))
        lo_p, hi_p = self.shares[i - 1], self.shares[i]
        lo_s, hi_s = self.sizes[i - 1], self.sizes[i]
        frac = (u - lo_p) / (hi_p - lo_p) if hi_p > lo_p else 0.0
        return max(1, min(self.cap, int(lo_s + frac * (hi_s - lo_s))))

    def mean(self) -> float:
        """Mean size, with draws above ``cap`` counted at ``cap``."""
        total = 0.0
        for i in range(1, len(self.sizes)):
            weight = self.shares[i] - self.shares[i - 1]
            lo, hi = min(self.sizes[i - 1], self.cap), min(self.sizes[i], self.cap)
            if self.sizes[i] <= self.cap or self.sizes[i - 1] >= self.cap:
                total += weight * (lo + hi) / 2
                continue
            below = (self.cap - self.sizes[i - 1]) / (self.sizes[i] - self.sizes[i - 1])
            total += weight * (below * (self.sizes[i - 1] + self.cap) / 2)
            total += weight * (1 - below) * self.cap
        return max(total, 1.0)


def _read_headers(conn: socket.socket) -> tuple[str, dict[str, str], bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            raise OSError("closed")
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in lines[1:]:
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return lines[0], headers, rest


def store(args: argparse.Namespace) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", args.port))
    server.listen(256)

    def handle(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(120)
            try:
                request, headers, body = _read_headers(conn)
                left = int(headers.get("content-length", "0")) - len(body)
                while left > 0:
                    chunk = conn.recv(min(CHUNK, left))
                    if not chunk:
                        return
                    left -= len(chunk)
                status = (
                    "201 Created"
                    if request.startswith("PUT ")
                    else "405 Method Not Allowed"
                )
                conn.sendall(f"HTTP/1.1 {status}\r\nContent-Length: 0\r\n\r\n".encode())
            except (OSError, ValueError):
                pass

    while True:
        conn, _ = server.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def upload(
    host: str,
    port: int,
    size: int,
    active: threading.Semaphore,
    upload_kbps: float,
) -> None:
    payload = bytes(min(size, CHUNK))
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as conn:
            if upload_kbps:
                rate = int(upload_kbps * 1000 / 8)
                conn.setsockopt(socket.SOL_SOCKET, SO_MAX_PACING_RATE, rate)
            # A rate-limited upload takes longer; allow for it.
            conn.settimeout(
                60 + (size * 8 / (upload_kbps * 1000) if upload_kbps else 0)
            )
            name = os.urandom(8).hex()
            conn.sendall(
                f"PUT /objects/{name} HTTP/1.1\r\nHost: {host}\r\n"
                "Content-Type: application/octet-stream\r\n"
                f"Content-Length: {size}\r\nConnection: close\r\n\r\n".encode()
            )
            left = size
            while left > 0:
                left -= conn.send(payload[: min(left, CHUNK)])
            _read_headers(conn)
    except OSError:
        pass
    finally:
        active.release()


def backup(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    sizes = ObjectSizes(args.object_mix, args.max_object_kb * 1024)
    stores: list[tuple[str, int]] = []
    weights: list[float] = []
    for text in args.store:
        url, _, weight = text.partition("=")
        host, _, port = url.removeprefix("http://").partition(":")
        stores.append((host, int(port or 80)))
        weights.append(float(weight or 1))
    rate = args.rate_kbps * 1000 / (8 * sizes.mean())
    depth = min(max(args.cycle_depth, 0.0), 1.0)
    peak = rate * (1 + depth) if args.cycle else rate
    active = threading.Semaphore(MAX_ACTIVE)
    start = time.monotonic()
    next_at = start + rng.expovariate(peak)
    while True:
        wait = next_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        # After a stall (a paused process) resume the schedule from now
        # instead of starting every missed upload at once.
        next_at = max(next_at, time.monotonic()) + rng.expovariate(peak)
        if args.cycle:
            phase = 2 * math.pi * (time.monotonic() - start) / args.cycle
            if rng.random() * peak > rate * (1 + depth * math.sin(phase)):
                continue
        host, port = rng.choices(stores, weights)[0]
        size = sizes.draw(rng)
        if not active.acquire(blocking=False):
            continue
        threading.Thread(
            target=upload,
            args=(host, port, size, active, args.upload_kbps),
            daemon=True,
        ).start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role", required=True)
    serve = sub.add_parser("store")
    serve.add_argument("--port", type=int, required=True)
    client = sub.add_parser("backup")
    client.add_argument("--store", action="append", required=True, help="URL=WEIGHT")
    client.add_argument("--rate-kbps", type=float, required=True)
    client.add_argument("--object-mix", choices=sorted(MIXES), default="web")
    client.add_argument("--max-object-kb", type=int, default=2048)
    client.add_argument("--upload-kbps", type=float, default=0.0)
    client.add_argument("--cycle", type=float, default=0.0)
    client.add_argument("--cycle-depth", type=float, default=0.5)
    client.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    while True:
        try:
            store(args) if args.role == "store" else backup(args)
        except OSError:
            time.sleep(5)


if __name__ == "__main__":
    main()
