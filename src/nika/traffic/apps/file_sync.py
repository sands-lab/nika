#!/usr/bin/env python3
"""Pull files from an HTTP file server over and over, like a sync client.

Each round downloads every URL once and discards the data, then waits a
random pause. Failed or stalled transfers are retried after a short backoff.
With ``--log`` every transfer gets one line: URL, bytes, seconds, and rate.
With ``--parallel`` every URL is synced on its own connection at the same
time, like a client with several sync folders.
"""

from __future__ import annotations

import argparse
import http.client
import os
import random
import threading
import time
from urllib.parse import urlsplit

CHUNK = 64 * 1024
LOG_MAX_BYTES = 1 << 20


def seconds_range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


def fetch(url: str, timeout: float) -> int:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    try:
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        conn.request("GET", target)
        response = conn.getresponse()
        if response.status != 200:
            raise OSError(f"HTTP {response.status}")
        size = 0
        while chunk := response.read(CHUNK):
            size += len(chunk)
        return size
    finally:
        conn.close()


def log_transfer(path: str, url: str, size: int, seconds: float, error: str) -> None:
    if not path:
        return
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    rate = size * 8 / seconds / 1000 if seconds > 0 else 0.0
    status = f"failed: {error}" if error else "ok"
    try:
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as log:
            log.write(
                f"{stamp} {url} {status} bytes={size} "
                f"seconds={seconds:.1f} rate={rate:.0f}kbit/s\n"
            )
    except OSError:
        pass


def sync(
    urls: list[str],
    pause: tuple[float, float],
    timeout: float,
    seed: int,
    log: str = "",
    spread: float | None = None,
) -> None:
    rng = random.Random(seed)
    # Clients that start together do not stay in lockstep.
    time.sleep(rng.uniform(0, min(pause[1], 10.0) if spread is None else spread))
    while True:
        for url in urls:
            started = time.monotonic()
            try:
                size = fetch(url, timeout)
            except Exception as exc:  # noqa: BLE001 - keep syncing after any failure
                log_transfer(log, url, 0, time.monotonic() - started, str(exc))
                time.sleep(rng.uniform(3, 10))
            else:
                log_transfer(log, url, size, time.monotonic() - started, "")
        time.sleep(rng.uniform(*pause))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--pause", default="2-5", help="seconds between rounds")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--parallel", action="store_true")
    parser.add_argument("--log", default="", help="per-transfer log file")
    parser.add_argument(
        "--start-spread",
        type=float,
        default=None,
        help="max random delay (s) before the first round (default: the "
        "longest pause, at most 10 s)",
    )
    args = parser.parse_args()
    pause = seconds_range(args.pause)
    if not args.parallel:
        sync(args.urls, pause, args.timeout, args.seed, args.log, args.start_spread)
        return
    for index, url in enumerate(args.urls):
        threading.Thread(
            target=sync,
            args=(
                [url],
                pause,
                args.timeout,
                args.seed + index,
                args.log,
                args.start_spread,
            ),
            daemon=True,
        ).start()
    threading.Event().wait()


if __name__ == "__main__":
    main()
