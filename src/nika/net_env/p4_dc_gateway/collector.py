#!/usr/bin/env python3
"""Parse P4.org INT-MD telemetry reports into bounded traces and port gauges."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import struct
import time
from pathlib import Path

MAX_OUTPUT_BYTES = 32 * 1024 * 1024
KEEP_OUTPUT_BYTES = 16 * 1024 * 1024
INT_UDP_PORT = 32767
REPORT_BUFFER_BYTES = 4 * 1024 * 1024


def reserve_report_buffer(sock: socket.socket) -> None:
    """Reserve the report burst buffer, including on kernels with a lower cap."""
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, REPORT_BUFFER_BYTES)
    # Linux returns twice the requested size, including socket bookkeeping.
    required = 2 * REPORT_BUFFER_BYTES
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) < required:
        # Linux UAPI SO_RCVBUFFORCE; Kathara nodes have CAP_NET_ADMIN.
        try:
            sock.setsockopt(socket.SOL_SOCKET, 33, REPORT_BUFFER_BYTES)
        except OSError as exc:
            raise RuntimeError("Cannot reserve the INT report receive buffer") from exc
    if sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) < required:
        raise RuntimeError("INT report receive buffer is below the required capacity")


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


def parse_report(raw: bytes, now_ns: int) -> dict:
    """Decode a v2.0 Inner Only/IPv4 report carrying v2.1 INT-MD.

    Baseline instructions 0-3 carry node, level-1 ports, latency in BMv2
    microseconds, and queue 0 occupancy in packets. Each hop prepends 16 bytes.
    Reports truncate after the original L4 header; IP lengths describe the
    untruncated data packet. Reject unsupported formats and truncated metadata.
    """
    if len(raw) < 56:
        raise ValueError("short report")
    group, node_id, report = struct.unpack_from("!III", raw)
    if group >> 28 != 2 or report >> 24 != 4:
        raise ValueError("expected v2 Inner Only/IPv4 report")
    words = (report >> 16) & 255
    if (words != 255 and len(raw) != 12 + words * 4) or (
        words == 255 and len(raw) < 1032
    ):
        raise ValueError("invalid report length")
    ip = 12
    if raw[ip] != 0x45 or raw[ip + 9] != 17:
        raise ValueError("expected IPv4/UDP INT encapsulation")
    if struct.unpack_from("!H", raw, ip + 22)[0] != INT_UDP_PORT:
        raise ValueError("unexpected INT UDP port")
    shim = ip + 28
    kind, length, _reserved, protocol = struct.unpack_from("!BBBB", raw, shim)
    if kind & 0xFC != 0x18 or protocol not in (6, 17):
        raise ValueError("unsupported INT shim")
    md = shim + 4
    flags, instructions, domain, _ds_instructions, _ds_flags = struct.unpack_from(
        "!IHHHH", raw, md
    )
    if (
        flags >> 28 != 2
        or (flags >> 8) & 31 != 4
        or instructions & 0xFF81 != 0xF000
        or domain
    ):
        raise ValueError("unsupported INT metadata instructions")
    if length < 7 or (length - 3) % 4 or length > 35:
        raise ValueError("invalid INT stack length")
    count = (length - 3) // 4
    l4 = md + 12 + count * 16
    l4_size = 20 if protocol == 6 else 8
    if l4 + l4_size > len(raw):
        raise ValueError("truncated INT stack or transport header")
    src, dst = (
        socket.inet_ntoa(raw[ip + 12 : ip + 16]),
        socket.inet_ntoa(raw[ip + 16 : ip + 20]),
    )
    src_port, dst_port = struct.unpack_from("!HH", raw, l4)
    identity = f"{src}|{dst}|{protocol}|{src_port}|{dst_port}".encode()
    hops = []
    for index in reversed(range(count)):
        switch, ingress, egress, latency, queue = struct.unpack_from(
            "!IHHII", raw, md + 12 + index * 16
        )
        hops.append(
            {
                "switch_id": switch,
                "ingress_port": ingress,
                "egress_port": egress,
                "hop_latency": latency,
                "queue_id": queue >> 24,
                "queue_occupancy": queue & 0xFFFFFF,
            }
        )
    packet_identity = identity + raw[ip + 4 : ip + 8] + raw[l4 : l4 + l4_size]
    complete = not (flags & (3 << 25)) and hops[-1]["switch_id"] == node_id
    return {
        "flow_id": hashlib.blake2b(identity, digest_size=8).hexdigest(),
        "packet_id": hashlib.blake2b(packet_identity, digest_size=12).hexdigest(),
        "report_id": f"{node_id}:{group & 0x3FFFFF}",
        "reporter_id": node_id,
        "report_point": "ingress" if (group >> 22) & 63 == 1 else "egress",
        "packet_timestamp": now_ns,
        "src": src,
        "dst": dst,
        "protocol": protocol,
        "src_port": src_port,
        "dst_port": dst_port,
        "ecn": raw[ip + 1] & 3,
        "m": (flags >> 25) & 1,
        "e": (flags >> 26) & 1,
        "hop_sequence": hops,
        "sink_seen": True,
        "trace_complete": complete,
    }


def port_metrics(ports: dict[tuple[str, int], dict]) -> str:
    """Render the latest packet's measurements per switch and egress port."""
    lines = [
        "# HELP p4_int_queue_depth_packets Queue occupancy at data packet dequeue.",
        "# TYPE p4_int_queue_depth_packets gauge",
        "# HELP p4_int_hop_latency_seconds Data packet egress minus ingress timestamp.",
        "# TYPE p4_int_hop_latency_seconds gauge",
        "# HELP p4_int_last_report_timestamp_seconds Collector receipt time.",
        "# TYPE p4_int_last_report_timestamp_seconds gauge",
    ]
    for (switch, port), hop in sorted(ports.items()):
        labels = f'switch="{switch}",egress_port="{port}"'
        lines.extend(
            [
                f"p4_int_queue_depth_packets{{{labels}}} {hop['queue_occupancy']}",
                f"p4_int_hop_latency_seconds{{{labels}}} {hop['hop_latency'] / 1_000_000}",
                f"p4_int_last_report_timestamp_seconds{{{labels}}} {hop['received_ns'] / 1_000_000_000}",
            ]
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=32766)
    parser.add_argument("--output", required=True)
    parser.add_argument("--metrics", default="/var/lib/node_exporter/int.prom")
    parser.add_argument(
        "--switches", required=True, help="JSON map of switch ID to name and data ports"
    )
    args = parser.parse_args()
    switches = json.loads(args.switches)
    output, metrics = Path(args.output), Path(args.metrics)
    output.parent.mkdir(parents=True, exist_ok=True)
    metrics.parent.mkdir(parents=True, exist_ok=True)
    ports: dict[tuple[str, int], dict] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    reserve_report_buffer(sock)
    sock.bind((args.listen, args.port))
    sock.settimeout(1)
    last_flush = 0.0
    while True:
        try:
            raw, _peer = sock.recvfrom(65535)
        except socket.timeout:
            raw = b""
        if raw:
            try:
                record = parse_report(raw, time.time_ns())
                for hop in record["hop_sequence"]:
                    switch = switches[str(hop["switch_id"])]
                    if hop["egress_port"] not in switch["ports"]:
                        raise ValueError("unknown egress port")
                reporter = switches[str(record["reporter_id"])]
                record["arrival_switches"] = (
                    [reporter["name"]] if record["report_point"] == "ingress" else []
                )
                record["sink_seen"] = record["report_point"] == "egress" and any(
                    hop["switch_id"] == record["reporter_id"]
                    and hop["egress_port"] in reporter["access_ports"]
                    for hop in record["hop_sequence"]
                )
                record["trace_complete"] = (
                    record["trace_complete"] and record["sink_seen"]
                )
                for hop in record["hop_sequence"]:
                    hop["switch"] = switches[str(hop["switch_id"])]["name"]
                with output.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                for hop in record["hop_sequence"]:
                    switch = switches[str(hop["switch_id"])]
                    ports[(switch["name"], hop["egress_port"])] = {
                        **hop,
                        "received_ns": record["packet_timestamp"],
                    }
                compact_output(output, MAX_OUTPUT_BYTES, KEEP_OUTPUT_BYTES)
            except (ValueError, KeyError, struct.error):
                continue
        if time.monotonic() - last_flush >= 1:
            tmp = metrics.with_suffix(".tmp")
            tmp.write_text(port_metrics(ports))
            tmp.replace(metrics)
            last_flush = time.monotonic()


if __name__ == "__main__":
    main()
