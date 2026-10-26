#!/usr/bin/env python3
"""Collect JSON INT-MX hop reports over UDP and preserve observed traces."""

from __future__ import annotations

import argparse
import json
import socket
import time
from collections import OrderedDict, deque
from pathlib import Path

# Bound in-flight state and the on-disk window so flood-rate INT cannot OOM the
# 256 MiB collector cgroup (JSONL page cache counts toward the limit).
MAX_TRACES = 4096
MAX_HOPS = 32
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
KEEP_OUTPUT_BYTES = 16 * 1024 * 1024
# Hop reports travel as separate datagrams and can arrive after the sink's
# report, so a trace keeps accepting hops for this long after it completes.
LATE_HOP_SECONDS = 10.0

TraceKey = tuple[str, str]


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


def _record(key: TraceKey, sequence: list[dict]) -> dict:
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
    traces: OrderedDict[TraceKey, list[dict]],
    hop: dict,
    *,
    completed: deque[tuple[float, TraceKey]] | None = None,
    now: float = 0.0,
    max_traces: int = MAX_TRACES,
    max_hops: int = MAX_HOPS,
    late_hop_seconds: float = LATE_HOP_SECONDS,
) -> list[dict]:
    """Merge ``hop`` into its trace and return the updated trace record.

    Every hop yields the trace's current record, so readers keep the latest one
    per packet and still see traces that never reached the sink. Completed
    traces stay open for ``late_hop_seconds`` to merge late hop reports; the
    least recently updated traces are dropped once ``max_traces`` are open.
    """
    if completed is not None:
        while completed and now - completed[0][0] > late_hop_seconds:
            traces.pop(completed.popleft()[1], None)
    key = (str(hop["flow_id"]), str(hop["packet_id"]))
    if key in traces:
        traces.move_to_end(key)
        sequence = traces[key]
    else:
        while len(traces) >= max_traces:
            traces.popitem(last=False)
        sequence = []
        traces[key] = sequence
    was_complete = any(bool(item.get("sink")) for item in sequence)
    sequence.append(hop)
    sequence.sort(key=lambda item: item.get("packet_timestamp") or 0)
    if len(sequence) > max_hops:
        del sequence[:-max_hops]
    record = _record(key, sequence)
    if len(sequence) >= max_hops:
        traces.pop(key, None)
    elif record["sink_seen"] and not was_complete and completed is not None:
        completed.append((now, key))
    return [record]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=32766)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    traces: OrderedDict[TraceKey, list[dict]] = OrderedDict()
    completed: deque[tuple[float, TraceKey]] = deque()
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
        records = append_hop(traces, hop, completed=completed, now=time.monotonic())
        with output.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        compact_output(output, MAX_OUTPUT_BYTES, KEEP_OUTPUT_BYTES)


if __name__ == "__main__":
    main()
