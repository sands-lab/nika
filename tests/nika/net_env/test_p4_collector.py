"""Unit tests for bounded INT collector retention."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

import pytest

from nika.net_env.p4_dc_gateway.collector import append_hop, compact_output

pytestmark = pytest.mark.unit


def _hop(flow: str, packet: str, *, sink: bool = False, switch_id: int = 1) -> dict:
    return {
        "flow_id": flow,
        "packet_id": packet,
        "packet_timestamp": 1_000,
        "src": "192.0.2.10",
        "dst": "10.0.1.11",
        "protocol": 6,
        "src_port": 20000,
        "dst_port": 80,
        "switch_id": switch_id,
        "sink": sink,
    }


def test_late_hops_merge_after_sink() -> None:
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    assert append_hop(traces, _hop("f", "1"), max_traces=8, max_hops=32) == []
    first = append_hop(
        traces, _hop("f", "1", sink=True, switch_id=2), max_traces=8, max_hops=32
    )
    assert first[0]["sink_seen"] is True
    assert ("f", "1") in traces
    late = append_hop(traces, _hop("f", "1", switch_id=3), max_traces=8, max_hops=32)
    assert {hop["switch_id"] for hop in late[0]["hop_sequence"]} == {1, 2, 3}


def test_oldest_incomplete_evicted_when_full() -> None:
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    for index in range(2):
        assert append_hop(traces, _hop("f", str(index)), max_traces=2) == []
    records = append_hop(traces, _hop("f", "2"), max_traces=2)
    assert records[0]["packet_id"] == "0"
    assert set(traces) == {("f", "1"), ("f", "2")}


def test_hop_cap_flushes_and_drops_key() -> None:
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    assert append_hop(traces, _hop("f", "1"), max_hops=2) == []
    records = append_hop(traces, _hop("f", "1", switch_id=2), max_hops=2)
    assert len(records[0]["hop_sequence"]) == 2
    assert traces == {}


def test_compact_output_keeps_trailing_window(tmp_path: Path) -> None:
    path = tmp_path / "int_reports.jsonl"
    path.write_text("a\n" + ("b" * 100) + "\nc\n", encoding="utf-8")
    compact_output(path, max_bytes=20, keep_bytes=20)
    text = path.read_text(encoding="utf-8")
    assert text.endswith("c\n")
    assert "a\n" not in text
