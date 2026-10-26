#!/usr/bin/env python3
"""Application client: call HTTP APIs on a schedule, like a user-facing app.

Each of ``--workers`` threads loops over the URLs, one request per URL, then
waits a random ``--every`` interval. With ``--body`` every request is a JSON
``POST``; otherwise it is a ``GET``. Every request opens a new connection and
the response body is read to the end. A failed request is retried with the
next round.
"""

from __future__ import annotations

import argparse
import http.client
import random
import threading
import time
from urllib.parse import urlsplit


def seconds_range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


def call(url: str, body: str | None, timeout: float) -> None:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=timeout)
    path = parts.path or "/"
    if parts.query:
        path += f"?{parts.query}"
    try:
        headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0"}
        if body is None:
            conn.request("GET", path, headers=headers)
        else:
            headers["Content-Type"] = "application/json"
            conn.request("POST", path, body=body.encode(), headers=headers)
        response = conn.getresponse()
        response.read()
        if response.status >= 400:
            raise OSError(f"HTTP {response.status}")
    finally:
        conn.close()


def worker(args: argparse.Namespace, seed: int) -> None:
    rng = random.Random(seed)
    every = seconds_range(args.every)
    # Workers started together do not stay in lockstep.
    time.sleep(rng.uniform(0, every[1]))
    while True:
        for url in args.urls:
            try:
                call(url, args.body, args.timeout)
            except Exception:  # noqa: BLE001 - keep calling after any failure
                time.sleep(rng.uniform(1, 3))
        time.sleep(rng.uniform(*every))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("urls", nargs="+")
    parser.add_argument("--body", help="JSON request body; POST instead of GET")
    parser.add_argument("--every", default="2-6", help="seconds between rounds")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    threads = [
        threading.Thread(target=worker, args=(args, args.seed * 1000 + index))
        for index in range(args.workers)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


if __name__ == "__main__":
    main()
