#!/usr/bin/env python3
"""Collect JSON INT-MX hop reports over UDP and preserve observed traces."""

from __future__ import annotations

import argparse
import json
import socket
from collections import OrderedDict
from pathlib import Path

# Bound in-flight state and the on-disk window so flood-rate INT cannot OOM the
# 256 MiB collector cgroup (JSONL page cache counts toward the limit).
MAX_TRACES = 4096
MAX_HOPS = 32
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
KEEP_OUTPUT_BYTES = 16 * 1024 * 1024


def compact_output(path: Path, max_bytes: int, keep_bytes: int) -> None:
    """Keep only the trailing window of ``path`` when it exceeds ``max_bytes``."""
    if not path.exists():
        return
    size = path.stat().st_size
    if size <= max_bytes:
        return
    keep = min(keep_bytes, size)
    with path.open("rb") as handle:
        handle.seek(-keep, 2)
        chunk = handle.read()
    newline = chunk.find(b"\n")
    if newline >= 0:
        chunk = chunk[newline + 1 :]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(chunk)
    tmp.replace(path)


def _record(key: tuple[str, str], sequence: list[dict]) -> dict:
    latest = sequence[-1] if sequence else {}
    sink_seen = any(bool(item.get("sink")) for item in sequence)
    return {
        "flow_id": key[0],
        "packet_id": key[1],
        "packet_timestamp": latest.get("packet_timestamp"),
        "src": latest.get("src"),
        "dst": latest.get("dst"),
        "protocol": latest.get("protocol"),
        "src_port": latest.get("src_port"),
        "dst_port": latest.get("dst_port"),
        "hop_sequence": list(sequence),
        "sink_seen": sink_seen,
        "trace_complete": sink_seen and bool(sequence),
    }


def append_hop(
    traces: OrderedDict[tuple[str, str], list[dict]],
    hop: dict,
    *,
    max_traces: int = MAX_TRACES,
    max_hops: int = MAX_HOPS,
) -> list[dict]:
    """Accumulate ``hop``; return records to persist (sink, hop-cap, or eviction)."""
    key = (str(hop["flow_id"]), str(hop["packet_id"]))
    records: list[dict] = []
    if key in traces:
        traces.move_to_end(key)
        sequence = traces[key]
    else:
        while len(traces) >= max_traces:
            old_key, old_sequence = traces.popitem(last=False)
            if old_sequence:
                records.append(_record(old_key, old_sequence))
        sequence = []
        traces[key] = sequence
    sequence.append(hop)
    if len(sequence) > max_hops:
        del sequence[:-max_hops]
    sink_seen = any(bool(item.get("sink")) for item in sequence)
    hop_capped = len(sequence) >= max_hops
    if sink_seen or hop_capped:
        records.append(_record(key, sequence))
    if hop_capped:
        traces.pop(key, None)
    return records


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=32766)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.listen, args.port))
    while True:
        raw, _peer = sock.recvfrom(65535)
        try:
            hop = json.loads(raw)
            if "flow_id" not in hop or "packet_id" not in hop:
                continue
        except (json.JSONDecodeError, TypeError):
            continue
        records = append_hop(traces, hop)
        if not records:
            continue
        with output.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        compact_output(output, MAX_OUTPUT_BYTES, KEEP_OUTPUT_BYTES)


if __name__ == "__main__":
    main()
