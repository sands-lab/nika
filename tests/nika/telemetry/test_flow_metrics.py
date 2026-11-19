"""Per-exporter counters preserve hop identity and replay closed captures once."""

import pytest

from nika.telemetry.flow_metrics import FlowCounters

pytestmark = pytest.mark.unit


def test_closed_capture_is_ingested_once_with_frozen_interface_names(tmp_path) -> None:
    capture = tmp_path / "nfcapd.20261115123400"
    capture.write_bytes(b"")
    counters = FlowCounters(
        {
            "edge": {
                "address": "172.28.0.1",
                "supported": True,
                "interfaces": {"2": {"name": "eth0"}},
            },
            "other": {"supported": False},
        }
    )
    rows = [["172.28.0.1", "6", "2", "2", "10", "1000", "2026-11-15 12:34:00.000"]]
    counters.ingest(capture, rows)
    counters.ingest(capture, rows)
    text = counters.render()
    assert 'netflow_bytes_total{node="edge",proto="6",in_if="eth0"} 1000' in text
    assert "once per hop" in text
    assert counters.coverage()["edge"]["newest_record"] == "2026-11-15 12:34:00.000"
    assert counters.coverage()["other"]["supported"] is False
    assert counters.coverage()["other"]["last_update"] is None
