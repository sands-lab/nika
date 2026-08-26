#!/usr/bin/env python3
"""Export observed per-hop metadata from BMv2 dataplane interfaces."""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import struct
import time

ETH_P_ALL = 3
PACKET_OUTGOING = 4
PROTO_INT_MX = 253


def parse_packet(frame: bytes) -> dict | None:
    if len(frame) < 34 or frame[12:14] != b"\x08\x00":
        return None
    ip = 14
    ihl = (frame[ip] & 0x0F) * 4
    if len(frame) < ip + ihl:
        return None
    protocol = frame[ip + 9]
    is_int = protocol == PROTO_INT_MX
    packet_id = struct.unpack("!H", frame[ip + 4 : ip + 6])[0]
    src = socket.inet_ntoa(frame[ip + 12 : ip + 16])
    dst = socket.inet_ntoa(frame[ip + 16 : ip + 20])
    offset = ip + ihl
    m = e = 0
    if protocol == PROTO_INT_MX:
        if len(frame) < offset + 7:
            return None
        flags = frame[offset + 1]
        m, e = (flags >> 7) & 1, (flags >> 6) & 1
        protocol = frame[offset + 2]
        offset += 7
    src_port = dst_port = 0
    if protocol in (6, 17) and len(frame) >= offset + 4:
        src_port, dst_port = struct.unpack("!HH", frame[offset : offset + 4])
    identity = f"{src}|{dst}|{protocol}|{src_port}|{dst_port}".encode()
    return {
        "flow_id": hashlib.blake2b(identity, digest_size=8).hexdigest(),
        "packet_id": str(packet_id),
        "src": src,
        "dst": dst,
        "protocol": protocol,
        "src_port": src_port,
        "dst_port": dst_port,
        "ecn": frame[ip + 1] & 0x03,
        "m": m,
        "e": e,
        "is_int": is_int,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--switch-id", type=int, required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--collector", required=True)
    parser.add_argument(
        "--ports", required=True, help="JSON map from eth name to BMv2 port"
    )
    args = parser.parse_args()
    ports = {str(k): int(v) for k, v in json.loads(args.ports).items()}
    capture = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    export = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pending: dict[tuple[str, str], tuple[int, int, dict]] = {}
    while True:
        frame, address = capture.recvfrom(65535)
        interface, _protocol, packet_type = address[:3]
        if interface not in ports:
            continue
        packet = parse_packet(frame)
        if packet is None:
            continue
        key = (packet["flow_id"], packet["packet_id"])
        now = time.time_ns()
        if packet_type != PACKET_OUTGOING:
            pending[key] = (ports[interface], now, packet)
            continue
        ingress_port, ingress_time, observed = pending.pop(key, (0, now, packet))
        if not (observed["is_int"] or packet["is_int"]):
            continue
        observed.pop("is_int", None)
        report = {
            **observed,
            "packet_timestamp": now,
            "switch_id": args.switch_id,
            "ingress_port": ingress_port,
            "egress_port": ports[interface],
            "ingress_timestamp": ingress_time,
            "egress_timestamp": now,
            "hop_latency": max(0, now - ingress_time),
            "queue_occupancy": 0,
            "sink": args.role == "leaf",
        }
        export.sendto(
            json.dumps(report, separators=(",", ":")).encode(), (args.collector, 32766)
        )


if __name__ == "__main__":
    main()
