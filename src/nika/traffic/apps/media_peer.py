#!/usr/bin/env python3
"""Real-time media endpoint: send and receive RTP streams over UDP.

Each ``--peer HOST:PORT/KBPS[/BYTES]`` is one call leg sent at a constant bit
rate, in RTP packets of BYTES payload (default ``--size``) marked DSCP EF, as
softphones and video endpoints mark media by default. A 160-byte payload is a
G.711 voice frame (PCMU, 8 kHz clock); other sizes are video (90 kHz clock).

Streams arriving on ``--port`` are counted like a softphone's call
statistics: every ``--stats-every`` seconds each stream gets one line in
``--log`` with the packets received, packets lost (RTP sequence gaps), and the
RFC 3550 interarrival jitter.
"""

from __future__ import annotations

import argparse
import os
import random
import re
import select
import socket
import struct
import time

DSCP_EF = 46
PCMU_PAYLOAD = 160
CLOCK_HZ = {0: 8000, 96: 90000}
LOG_MAX_BYTES = 1 << 20

_STATS_RE = re.compile(
    r"^(?P<time>\S+) rtp from (?P<source>[\d.]+):\d+ .*?"
    r"rx=(?P<rx>\d+) lost=(?P<lost>\d+) loss=(?P<loss>[\d.]+)% "
    r"jitter=(?P<jitter>[\d.]+)ms"
)


def parse_stats(line: str) -> dict[str, object] | None:
    """One ``--log`` line as ``{time, source, rx, lost, loss, jitter}``."""
    match = _STATS_RE.match(line.strip())
    if match is None:
        return None
    return {
        "time": match["time"],
        "source": match["source"],
        "rx": int(match["rx"]),
        "lost": int(match["lost"]),
        "loss": float(match["loss"]),
        "jitter": float(match["jitter"]),
    }


def parse_peer(text: str, size: int) -> tuple[tuple[str, int], float, int]:
    address, _, rest = text.partition("/")
    kbps, _, payload = rest.partition("/")
    host, _, port = address.rpartition(":")
    return (host, int(port)), float(kbps or 80), int(payload or size)


class Stream:
    """Receive-side counters of one RTP source (RFC 3550 A.1, A.8)."""

    def __init__(self, seq: int) -> None:
        self.max_seq = seq
        self.cycles = 0
        self.prior_ext = seq - 1
        self.received = 0
        self.expected_hint = 0
        self.idle = 0
        self.transit: float | None = None
        self.jitter = 0.0
        self.clock = 90000

    def packet(self, seq: int, ts: int, pt: int, arrival: float) -> None:
        delta = (seq - self.max_seq) & 0xFFFF
        if delta < 0x8000:
            if seq < self.max_seq:
                self.cycles += 1 << 16
            self.max_seq = seq
        self.received += 1
        self.clock = CLOCK_HZ.get(pt, 90000)
        transit = arrival * self.clock - ts
        if self.transit is not None:
            diff = transit - self.transit
            # RTP timestamps wrap at 2**32; keep the difference small.
            diff = (diff + (1 << 31)) % (1 << 32) - (1 << 31)
            self.jitter += (abs(diff) - self.jitter) / 16
        self.transit = transit

    def interval(self) -> tuple[int, int, float]:
        """Packets received and lost since the last call, and jitter in ms."""
        ext = self.cycles + self.max_seq
        expected = ext - self.prior_ext
        if self.received == 0:
            # Nothing arrived: count the last interval's packets as lost.
            expected = self.expected_hint
            self.idle += 1
        else:
            self.idle = 0
            self.expected_hint = expected
        received, self.received, self.prior_ext = self.received, 0, ext
        return received, max(expected - received, 0), self.jitter * 1000 / self.clock


def write_log(path: str, lines: list[str]) -> None:
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as log:
            log.writelines(line + "\n" for line in lines)
    except OSError:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=16384)
    parser.add_argument("--peer", action="append", default=[])
    parser.add_argument("--size", type=int, default=200, help="RTP payload bytes")
    parser.add_argument("--log", default="", help="call statistics log file")
    parser.add_argument("--stats-every", type=float, default=10.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    rng = random.Random(args.seed)
    while True:
        try:
            run(args, rng)
        except OSError:
            time.sleep(5)


def run(args: argparse.Namespace, rng: random.Random) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DSCP_EF << 2)
    sock.bind(("", args.port))
    sock.setblocking(False)
    legs = []
    now = time.monotonic()
    for text in args.peer:
        address, kbps, size = parse_peer(text, args.size)
        interval = (size + 12) * 8 / (kbps * 1000)
        pt = 0 if size == PCMU_PAYLOAD else 96
        legs.append(
            {
                "address": address,
                "payload": bytes(size),
                "interval": interval,
                "next": now + rng.uniform(0, interval),
                "start": now,
                "pt": pt,
                "clock": CLOCK_HZ[pt],
                "seq": rng.randrange(1 << 16),
                "ts": rng.randrange(1 << 32),
                "ssrc": rng.randrange(1 << 32),
            }
        )
    streams: dict[tuple[tuple[str, int], int], Stream] = {}
    next_stats = now + args.stats_every
    while True:
        wait = min((leg["next"] for leg in legs), default=now + 1.0)
        timeout = max(0.0, min(wait, next_stats) - time.monotonic())
        readable, _, _ = select.select([sock], [], [], timeout)
        if readable:
            for _ in range(256):
                try:
                    data, source = sock.recvfrom(4096)
                except BlockingIOError:
                    break
                if len(data) < 12:
                    continue
                _, marker_pt, seq, ts, ssrc = struct.unpack("!BBHII", data[:12])
                key = (source, ssrc)
                stream = streams.get(key)
                if stream is None:
                    stream = streams[key] = Stream(seq)
                stream.packet(seq, ts, marker_pt & 0x7F, time.monotonic())
        now = time.monotonic()
        for leg in legs:
            # Send every packet that is due; after a long stall skip ahead.
            if now - leg["next"] > 1.0:
                leg["next"] = now
            while leg["next"] <= now:
                sampled = int((leg["next"] - leg["start"]) * leg["clock"])
                header = struct.pack(
                    "!BBHII",
                    0x80,
                    leg["pt"],
                    leg["seq"],
                    (leg["ts"] + sampled) & 0xFFFFFFFF,
                    leg["ssrc"],
                )
                try:
                    sock.sendto(header + leg["payload"], leg["address"])
                except (BlockingIOError, OSError):
                    pass
                leg["seq"] = (leg["seq"] + 1) & 0xFFFF
                leg["next"] += leg["interval"]
        if now >= next_stats:
            next_stats = now + args.stats_every
            if args.log and streams:
                stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                lines = []
                for (source, ssrc), stream in streams.items():
                    rx, lost, jitter = stream.interval()
                    loss = 100.0 * lost / (rx + lost) if rx + lost else 0.0
                    lines.append(
                        f"{stamp} rtp from {source[0]}:{source[1]} "
                        f"ssrc={ssrc:08x} rx={rx} lost={lost} loss={loss:.1f}% "
                        f"jitter={jitter:.1f}ms"
                    )
                write_log(args.log, lines)
            # A call that stayed silent for a minute has ended.
            for key in [
                k for k, v in streams.items() if v.idle * args.stats_every >= 60
            ]:
                del streams[key]


if __name__ == "__main__":
    main()
