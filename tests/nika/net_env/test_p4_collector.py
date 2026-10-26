"""Unit tests for bounded INT collector retention."""

from __future__ import annotations

from collections import OrderedDict, deque
from pathlib import Path

import pytest

from nika.net_env.p4_dc_gateway.collector import append_hop, compact_output

pytestmark = pytest.mark.unit


def _hop(
    flow: str, packet: str, *, sink: bool = False, switch_id: int = 1, ts: int = 1_000
) -> dict:
    return {
        "flow_id": flow,
        "packet_id": packet,
        "packet_timestamp": ts,
        "src": "192.0.2.10",
        "dst": "10.0.1.11",
        "protocol": 6,
        "src_port": 20000,
        "dst_port": 80,
        "switch_id": switch_id,
        "sink": sink,
    }


def test_every_hop_persists_current_trace() -> None:
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    (partial,) = append_hop(traces, _hop("f", "1"))
    assert partial["sink_seen"] is False
    assert partial["trace_complete"] is False
    (full,) = append_hop(traces, _hop("f", "1", sink=True, switch_id=2, ts=2_000))
    assert full["trace_complete"] is True
    assert [hop["switch_id"] for hop in full["hop_sequence"]] == [1, 2]


def test_late_hops_merge_after_sink_until_expiry() -> None:
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    completed: deque = deque()
    append_hop(
        traces,
        _hop("f", "1", sink=True, switch_id=2, ts=2_000),
        completed=completed,
        now=0.0,
    )
    (late,) = append_hop(
        traces, _hop("f", "1", switch_id=1, ts=1_000), completed=completed, now=5.0
    )
    assert [hop["switch_id"] for hop in late["hop_sequence"]] == [1, 2]
    assert late["trace_complete"] is True
    append_hop(traces, _hop("g", "1"), completed=completed, now=11.0)
    assert ("f", "1") not in traces


def test_oldest_trace_dropped_when_full() -> None:
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    for index in range(3):
        append_hop(traces, _hop("f", str(index)), max_traces=2)
    assert set(traces) == {("f", "1"), ("f", "2")}


def test_hop_cap_flushes_and_drops_key() -> None:
    traces: OrderedDict[tuple[str, str], list[dict]] = OrderedDict()
    append_hop(traces, _hop("f", "1"), max_hops=2)
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
