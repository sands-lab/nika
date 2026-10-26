#!/usr/bin/env python3
"""Partition/aggregate query service over UDP.

``worker``: a shard. It answers every query datagram with the number of
datagrams the query asks for, sent back to back to the asking address, the way
a cache multiget over UDP returns its values.

``aggregator``: a front end. Every ``--period`` seconds it sends one query to
each worker at once and reads their answers until the next query. Because the
workers answer the same query at the same moment, their replies arrive at the
aggregator together.
"""

from __future__ import annotations

import argparse
import random
import select
import socket
import struct
import time

HEADER = struct.Struct("!IH")


def seconds_range(text: str) -> tuple[float, float]:
    low, _, high = text.partition("-")
    return float(low), float(high or low)


def worker(args: argparse.Namespace) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", args.port))
    while True:
        data, address = sock.recvfrom(2048)
        try:
            _tag, seq, count, size = data.decode("ascii").split()
            seq, count, size = int(seq), min(int(count), 256), min(int(size), 1472)
        except ValueError:
            continue
        padding = bytes(max(0, size - HEADER.size))
        for index in range(count):
            try:
                sock.sendto(HEADER.pack(seq, index) + padding, address)
            except OSError:
                break


def aggregator(args: argparse.Namespace) -> None:
    rng = random.Random(args.seed)
    period = seconds_range(args.period)
    workers = []
    for text in args.worker:
        host, _, port = text.rpartition(":")
        workers.append((host, int(port)))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind(("", args.port))
    seq = 0
    query_at = time.monotonic() + rng.uniform(*period)
    while True:
        wait = query_at - time.monotonic()
        if wait > 0:
            readable, _, _ = select.select([sock], [], [], wait)
            if readable:
                sock.recv(2048)
            continue
        seq = (seq + 1) & 0xFFFFFFFF
        query = f"q {seq} {args.count} {args.size}".encode("ascii")
        for address in workers:
            try:
                sock.sendto(query, address)
            except OSError:
                pass
        query_at += rng.uniform(*period)
        query_at = max(query_at, time.monotonic())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="role", required=True)
    serve = sub.add_parser("worker")
    serve.add_argument("--port", type=int, default=11311)
    front = sub.add_parser("aggregator")
    front.add_argument("--worker", action="append", required=True, help="HOST:PORT")
    front.add_argument("--port", type=int, default=11311)
    front.add_argument("--count", type=int, default=24, help="datagrams per answer")
    front.add_argument("--size", type=int, default=1400, help="datagram payload")
    front.add_argument("--period", default="1-2", help="seconds between queries")
    front.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    while True:
        try:
            worker(args) if args.role == "worker" else aggregator(args)
        except OSError:
            time.sleep(5)


if __name__ == "__main__":
    main()
