"""Collector-side queries preserve delivery evidence and bound transferred data."""

import json
import subprocess
from unittest.mock import Mock

import pytest

from nika.service.kathara import telemetry_api

pytestmark = pytest.mark.unit


def test_query_pages_after_merging_ingress_reports(tmp_path, monkeypatch):
    reports = tmp_path / "reports.jsonl"
    source = {
        "flow_id": "f",
        "packet_id": "a",
        "packet_timestamp": 1,
        "trace_complete": False,
        "hop_sequence": [{"switch": "spine_1", "egress_port": 10}],
    }
    rows = [
        {**source, "packet_id": f"unrelated-{i}", "hop_sequence": []}
        for i in range(1000)
    ]
    rows += [
        source,
        {**source, "hop_sequence": [], "arrival_switches": ["leaf_2"]},
        {**source, "packet_id": "b"},
        {**source, "packet_id": "c", "trace_complete": True, "sink_seen": True},
    ]
    reports.write_text("\n".join(map(json.dumps, rows)) + "\ninvalid line\n")
    monkeypatch.setattr(telemetry_api, "INT_REPORTS", str(reports))
    transferred = []

    def execute(_execute, node, command, *, timeout):
        assert node == "collector" and timeout == 20
        result = subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True, check=True
        ).stdout
        transferred.append(len(result))
        return result

    monkeypatch.setattr(telemetry_api, "checked_exec", execute)
    api = telemetry_api.TelemetryAPIMixin()
    api.exec_cmd = Mock()
    query = dict(start_time="0", egress_switch="spine_1", egress_port=10, limit=2)
    assert [r["packet_id"] for r in api.int_query_telemetry(**query)] == ["b", "c"]
    older = api.int_query_telemetry(**query, offset=1)
    assert [r["packet_id"] for r in older] == ["a", "b"]
    assert older[0]["arrival_switches"] == ["leaf_2"]
    assert max(transferred) < reports.stat().st_size / 100
    assert api.int_query_telemetry(**{**query, "start_time": "9999999999"}) == []
