"""INT wire-format parsing, per-port metrics, and bounded trace retention."""

from __future__ import annotations

import socket
import struct
from pathlib import Path

import pytest

from nika.net_env.p4_dc_gateway.collector import (
    REPORT_BUFFER_BYTES,
    compact_output,
    parse_report,
    port_metrics,
    reserve_report_buffer,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kernel_cap", [212992, REPORT_BUFFER_BYTES])
def test_report_buffer_preserves_capacity_under_kernel_cap(kernel_cap: int) -> None:
    class ReportSocket:
        capacity = 0

        def setsockopt(self, level, option, size):
            assert level == socket.SOL_SOCKET
            self.capacity = 2 * (
                min(size, kernel_cap) if option == socket.SO_RCVBUF else size
            )

        def getsockopt(self, level, option):
            return self.capacity

    sock = ReportSocket()
    reserve_report_buffer(sock)
    assert sock.capacity == 2 * REPORT_BUFFER_BYTES


@pytest.mark.parametrize("reject_force", [False, True])
def test_report_buffer_rejects_unavailable_capacity(reject_force: bool) -> None:
    class ReportSocket:
        def setsockopt(self, level, option, size):
            if option != socket.SO_RCVBUF and reject_force:
                raise PermissionError("CAP_NET_ADMIN unavailable")

        def getsockopt(self, level, option):
            return 425984

    with pytest.raises(RuntimeError, match="receive buffer"):
        reserve_report_buffer(ReportSocket())


def report_packet() -> bytes:
    # P4.org report v2.0, Inner Only/IPv4, 27-word INT-MD TCP fragment.
    ip = bytearray(20)
    ip[0], ip[1], ip[9] = 0x45, 3, 17
    ip[12:16] = socket.inet_aton("192.0.2.10")
    ip[16:20] = socket.inet_aton("10.0.1.11")
    udp = struct.pack("!HHHH", 20000, 32767, 0, 0)
    shim = bytes([0x18, 15, 0, 6])  # 3 MD words + 3 * 4 hop words
    md = struct.pack("!IHHHH", 0x20000405, 0xF000, 0, 0, 0)
    # Transit/sink prepend; the wire stack has sink first.
    hops = b"".join(
        struct.pack("!IHHII", node, ingress, egress, latency, depth)
        for node, ingress, egress, latency, depth in [
            (201, 2, 3, 21000, 7),
            (101, 1, 3, 13000, 4),
            (1, 1, 2, 7000, 2),
        ]
    )
    tcp = struct.pack("!HH", 20000, 80) + bytes(16)
    inner = bytes(ip) + udp + shim + md + hops + tcp
    assert len(inner) == 112
    return struct.pack("!III", 0x20000009, 201, 0x041C0020) + inner


def test_standard_report_recovers_path_and_real_measurements() -> None:
    trace = parse_report(report_packet(), 123456)
    assert trace["packet_timestamp"] == 123456
    assert trace["report_id"] == "201:9"
    assert len(trace["packet_id"]) == 24
    assert trace["trace_complete"]
    assert (trace["src_port"], trace["dst_port"], trace["protocol"]) == (20000, 80, 6)
    hops = trace["hop_sequence"]
    assert [h["switch_id"] for h in hops] == [1, 101, 201]
    assert [h["queue_occupancy"] for h in hops] == [2, 4, 7]
    assert [h["hop_latency"] for h in hops] == [7000, 13000, 21000]
    assert trace["ecn"] == 3


@pytest.mark.parametrize("end", [0, 11, 55, 70, 123])
def test_truncated_reports_are_rejected(end: int) -> None:
    with pytest.raises(ValueError):
        parse_report(report_packet()[:end], 1)


@pytest.mark.parametrize(
    "offset,value", [(0, 0x10), (8, 0x14), (40, 0x10), (44, 0x10), (48, 0x00)]
)
def test_unsupported_formats_are_rejected(offset: int, value: int) -> None:
    raw = bytearray(report_packet())
    raw[offset] = value
    with pytest.raises(ValueError):
        parse_report(raw, 1)


def test_mtu_or_hop_limit_marks_trace_incomplete() -> None:
    raw = bytearray(report_packet())
    raw[44] |= 2  # MD M bit.
    trace = parse_report(raw, 1)
    assert not trace["trace_complete"]
    assert trace["m"] == 1


def test_metrics_have_bounded_port_labels_and_seconds() -> None:
    text = port_metrics(
        {
            ("spine_1", 3): {
                "queue_occupancy": 4,
                "hop_latency": 13000,
                "received_ns": 2_000_000_000,
            }
        }
    )
    assert 'p4_int_queue_depth_packets{switch="spine_1",egress_port="3"} 4' in text
    assert 'p4_int_hop_latency_seconds{switch="spine_1",egress_port="3"} 0.013' in text
    assert "flow" not in text and "packet_id" not in text


def test_compact_output_keeps_trailing_window(tmp_path: Path) -> None:
    path = tmp_path / "int_reports.jsonl"
    path.write_text("a\n" + ("b" * 100) + "\nc\n", encoding="utf-8")
    compact_output(path, max_bytes=20, keep_bytes=20)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("c\n")
    assert "a\n" not in text


def test_reserved_fields_are_ignored_on_reception() -> None:
    raw = bytearray(report_packet())
    raw[10] = 255  # MD Length is ignored for Inner Only reports.
    raw[40] |= 3  # Shim reserved bits.
    raw[42] = 255  # Reserved next-protocol byte.
    raw[45] = 255  # INT-MD reserved bits.
    raw[49] = 0x40  # Reserved baseline instruction bit.
    raw[52:56] = bytes([255] * 4)  # Domain zero has no DS instructions.
    trace = parse_report(raw, 1)
    assert trace["trace_complete"]
    assert [hop["queue_occupancy"] for hop in trace["hop_sequence"]] == [2, 4, 7]


def test_packet_identity_correlates_exporters_and_distinguishes_transport_packets():
    raw = report_packet()
    other_reporter = bytearray(raw)
    struct.pack_into("!II", other_reporter, 0, 0x20000011, 101)
    assert (
        parse_report(bytes(other_reporter), 2)["packet_id"]
        == parse_report(raw, 1)["packet_id"]
    )
    other_packet = bytearray(raw)
    other_packet[-16] = 1  # TCP sequence field, independent of hop/exporter metadata.
    assert (
        parse_report(bytes(other_packet), 3)["packet_id"]
        != parse_report(raw, 1)["packet_id"]
    )
