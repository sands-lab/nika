"""Summary-first ordering of the P4 and ONOS operator outputs."""

from __future__ import annotations

import json

import pytest

from nika.mcp.tool_output import truncate_tool_text
from nika.service.kathara.sdn_api import onos_summary

pytestmark = pytest.mark.unit


def test_onos_summary_leads_and_filters_device() -> None:
    flows = [
        {"deviceId": "of:1", "state": "ADDED"},
        {"deviceId": "of:1", "state": "PENDING_ADD"},
        {"deviceId": "of:2", "state": "ADDED"},
    ]
    summary, body = onos_summary({"flows": flows}, "of:1")
    assert summary["flows"]["count"] == 2
    assert summary["flows"]["states"] == {"state=ADDED": 1, "state=PENDING_ADD": 1}
    assert len(body["flows"]) == 2


def test_summary_survives_truncation() -> None:
    summary, body = onos_summary(
        {"flows": [{"deviceId": f"of:{i}", "state": "ADDED"} for i in range(2000)]}
    )
    text = json.dumps({"summary": summary, "body": body}, indent=2)
    assert len(text) > 10000
    assert '"count": 2000' in truncate_tool_text(text, max_chars=10000)


def test_p4rt_read_summary_precedes_switches() -> None:
    pytest.importorskip("p4.v1.p4runtime_pb2")
    from nika.net_env.utils.kathara import p4rt_manager

    observed = {
        "pipeline": {"ok": True, "matches_intent": True},
        "members": [1],
        "groups": [1, 2],
        "ipv4_lpm": [],
        "counters": {"ingress": {1: {"packets": 3}}, "egress": {}},
    }
    row = p4rt_manager.switch_summary(observed, ["groups"])
    assert row["pipeline_ok"] and row["groups"] == 2 and row["mismatches"] == ["groups"]
