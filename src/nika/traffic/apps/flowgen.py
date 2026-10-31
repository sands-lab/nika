#!/usr/bin/env python3
"""Open-loop TCP flows drawn from an empirical flow-size distribution.

``sink``: accepts TCP connections and reads each one to its end.

``send``: starts flows to the ``--dst`` sinks as a Poisson process whose rate
gives ``--load-kbps`` of offered load on average. Each flow picks a sink in
proportion to its weight, draws its size from the ``--sizes`` distribution
(clipped at ``--max-kb``), connects, writes the bytes, and closes. With
``--diurnal-period`` the rate follows a sine of that period and relative
``--diurnal-amplitude`` (a non-homogeneous Poisson process). With
``--flow-kbps`` the kernel paces each flow to that rate, so one flow cannot
fill a slow link on its own.

The distributions are the flow-size CDFs that data center transport studies
replay (pFabric, HPCC, Homa): ``websearch`` (DCTCP, SIGCOMM 2010),
``datamining`` (VL2, SIGCOMM 2009), and ``hadoop`` (Facebook Hadoop, Roy et
al., SIGCOMM 2015).
"""

from __future__ import annotations

import argparse
import bisect
import math
import random
import socket
import threading
import time

CHUNK = 64 * 1024
CONNECT_TIMEOUT = 5.0
# Flows a sender keeps open at once; arrivals beyond it are skipped, so a
# blackholed path cannot pile up threads.
MAX_ACTIVE = 64

# (bytes, cumulative probability)
CDFS = {
    "websearch": (
        (0, 0.0), (10_000, 0.15), (20_000, 0.20), (30_000, 0.30),
        (50_000, 0.40), (80_000, 0.53), (200_000, 0.60), (1_000_000, 0.70),
        (2_000_000, 0.80), (5_000_000, 0.90), (10_000_000, 0.97),
        (30_000_000, 1.0),
    ),
    "datamining": (
        (0, 0.0), (180, 0.1), (216, 0.2), (560, 0.3), (900, 0.4),
        (1_100, 0.5), (1_870, 0.6), (3_160, 0.7), (10_000, 0.8),
        (400_000, 0.9), (3_160_000, 0.95), (100_000_000, 0.98),
        (1_000_000_000, 1.0),
    ),
    "hadoop": (
        (0, 0.0), (100, 0.01), (200, 0.02), (300, 0.05), (350, 0.15),
        (400, 0.20), (500, 0.30), (600, 0.40), (700, 0.50), (1_000, 0.60),
        (2_000, 0.67), (7_000, 0.70), (30_000, 0.72), (50_000, 0.82),
        (80_000, 0.87), (120_000, 0.90), (300_000, 0.95), (1_000_000, 0.975),
        (2_000_000, 0.99), (10_000_000, 1.0),
    ),
}  # fmt: skip


class FlowSizes:
    """Inverse-transform sampling of a piecewise-linear CDF, clipped at ``cap``."""

    def __init__(self, name: str, cap: int) -> None:
        points = CDFS[name]
        self.sizes = [float(size) for size, _ in points]
        self.probs = [prob for _, prob in points]
        self.cap = cap

    def draw(self, rng: random.Random) -> int:
        u = rng.random()
        i = max(1, bisect.bisect_left(self.probs, u))
        lo_p, hi_p = self.probs[i - 1], self.probs[i]
        lo_s, hi_s = self.sizes[i - 1], self.sizes[i]
        frac = (u - lo_p) / (hi_p - lo_p) if hi_p > lo_p else 0.0
        return max(1, min(self.cap, int(lo_s + frac * (hi_s - lo_s))))

    def mean(self) -> float:
        """Mean of the clipped distribution (exact for the linear segments)."""
        total = 0.0
        for i in range(1, len(self.sizes)):
            weight = self.probs[i] - self.probs[i - 1]
            lo, hi = min(self.sizes[i - 1], self.cap), min(self.sizes[i], self.cap)
            if self.sizes[i] <= self.cap or self.sizes[i - 1] >= self.cap:
                total += weight * (lo + hi) / 2
                continue
            # Segment crosses the cap: uniform part below it, the rest at it.
            below = (self.cap - self.sizes[i - 1]) / (self.sizes[i] - self.sizes[i - 1])
            total += weight * (below * (self.sizes[i - 1] + self.cap) / 2)
            total += weight * (1 - below) * self.cap
        return max(total, 1.0)


def sink(args: argparse.Namespace) -> None:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("", args.port))
    server.listen(256)

    def drain(conn: socket.socket) -> None:
        with conn:
            conn.settimeout(120)
            try:
                while conn.recv(CHUNK):
                    pass
            except OSError:
                pass

    while True:
        conn, _ = server.accept()
        threading.Thread(target=drain, args=(conn,), daemon=True).start()


SO_MAX_PACING_RATE = getattr(socket, "SO_MAX_PACING_RATE", 47)


def send_flow(
    host: str, port: int, size: int, active: threading.Semaphore, flow_kbps: float
) -> None:
    payload = bytes(min(size, CHUNK))
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as conn:
            if flow_kbps:
                rate = int(flow_kbps * 1000 / 8)
                conn.setsockopt(socket.SOL_SOCKET, SO_MAX_PACING_RATE, rate)
            # A paced flow takes longer; allow for it before giving up.
            conn.settimeout(60 + (size * 8 / (flow_kbps * 1000) if flow_kbps else 0))
            left = size
            while left > 0:
                sent = conn.send(payload[: min(left, CHUNK)])
                left -= sent
            conn.shutdown(socket.SHUT_WR)
            while conn.recv(CHUNK):
                pass
    except OSError:
        pass
    finally:
        active.release()


def send(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    sizes = FlowSizes(args.sizes, args.max_kb * 1024)
    targets: list[tuple[str, int]] = []
    weights: list[float] = []
    for text in args.dst:
        address, _, weight = text.partition("/")
        host, _, port = address.rpartition(":")
        targets.append((host, int(port)))
        weights.append(float(weight or 1))
    rate = args.load_kbps * 1000 / (8 * sizes.mean())
    amplitude = min(max(args.diurnal_amplitude, 0.0), 1.0)
    peak = rate * (1 + amplitude) if args.diurnal_period else rate
    active = threading.Semaphore(MAX_ACTIVE)
    start = time.monotonic()
    # Start at a random phase so senders do not fire in step.
    next_at = start + rng.expovariate(peak)
    while True:
        wait = next_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        # After a stall (a paused process) resume the schedule from now
        # instead of firing every missed arrival at once.
        next_at = max(next_at, time.monotonic()) + rng.expovariate(peak)
        if args.diurnal_period:
            phase = 2 * math.pi * (time.monotonic() - start) / args.diurnal_period
            if rng.random() * peak > rate * (1 + amplitude * math.sin(phase)):
                continue
        host, port = rng.choices(targets, weights)[0]
        size = sizes.draw(rng)
        if not active.acquire(blocking=False):
            continue
        threading.Thread(
            target=send_flow,
            args=(host, port, size, active, args.flow_kbps),
            daemon=True,
        ).start()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role", required=True)
    serve = sub.add_parser("sink")
    serve.add_argument("--port", type=int, required=True)
    client = sub.add_parser("send")
    client.add_argument("--dst", action="append", required=True, help="IP:PORT/WEIGHT")
    client.add_argument("--load-kbps", type=float, required=True)
    client.add_argument("--sizes", choices=sorted(CDFS), default="websearch")
    client.add_argument("--max-kb", type=int, default=2048)
    client.add_argument("--flow-kbps", type=float, default=0.0)
    client.add_argument("--diurnal-period", type=float, default=0.0)
    client.add_argument("--diurnal-amplitude", type=float, default=0.5)
    client.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    while True:
        try:
            sink(args) if args.role == "sink" else send(args)
        except OSError:
            time.sleep(5)


if __name__ == "__main__":
    main()
