#!/usr/bin/env python3
"""Collect JSON INT-MX hop reports over UDP and preserve observed traces."""

from __future__ import annotations

import argparse
import json
import socket
from collections import defaultdict
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=32766)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    traces: dict[tuple[str, str], list[dict]] = defaultdict(list)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.listen, args.port))
    while True:
        raw, _peer = sock.recvfrom(65535)
        try:
            hop = json.loads(raw)
            key = (str(hop["flow_id"]), str(hop["packet_id"]))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        traces[key].append(hop)
        record = {
            "flow_id": key[0],
            "packet_id": key[1],
            "packet_timestamp": hop.get("packet_timestamp"),
            "src": hop.get("src"),
            "dst": hop.get("dst"),
            "protocol": hop.get("protocol"),
            "src_port": hop.get("src_port"),
            "dst_port": hop.get("dst_port"),
            "hop_sequence": traces[key],
            "sink_seen": any(bool(item.get("sink")) for item in traces[key]),
        }
        record["trace_complete"] = record["sink_seen"] and bool(record["hop_sequence"])
        with output.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
