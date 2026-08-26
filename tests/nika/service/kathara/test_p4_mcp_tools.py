"""Contracts for the P4Runtime and INT-MX MCP tools."""

from __future__ import annotations

import json

import pytest

from nika.service.mcp_server.kathara import bmv2_server, telemetry_server
from nika.service.kathara.bmv2_api import KatharaBMv2API
from nika.service.mcp_server.common import host_server


@pytest.mark.asyncio
async def test_p4_mcp_tool_schemas() -> None:
    bmv2_tools = {tool.name: tool for tool in await bmv2_server.mcp.list_tools()}
    telemetry_tools = {
        tool.name: tool for tool in await telemetry_server.mcp.list_tools()
    }

    assert set(bmv2_tools) == {"p4_get_runtime_state"}
    assert bmv2_tools["p4_get_runtime_state"].inputSchema["properties"] == {
        "switch_name": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "title": "Switch Name",
        }
    }
    assert set(telemetry_tools) == {"int_query_telemetry"}
    schema = telemetry_tools["int_query_telemetry"].inputSchema
    assert schema["required"] == ["start_time"]
    assert {
        "start_time",
        "end_time",
        "src",
        "dst",
        "protocol",
        "src_port",
        "dst_port",
        "flow_id",
        "packet_id",
        "limit",
    } == set(schema["properties"])


def test_p4_get_runtime_state_tool_calls_session_api(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    class FakeAPI:
        def __init__(self, lab_name: str) -> None:
            self.lab_name = lab_name

        def p4_get_runtime_state(self, switch_name: str | None = None) -> dict:
            calls.append((self.lab_name, switch_name))
            return {"switches": {"gateway_1": {"pipeline": {"ok": True}}}}

    monkeypatch.setattr(bmv2_server, "get_lab_name", lambda: "p4_dc_gateway_test")
    monkeypatch.setattr(bmv2_server, "KatharaBMv2API", FakeAPI)

    result = json.loads(bmv2_server.p4_get_runtime_state("gateway_1"))
    assert calls == [("p4_dc_gateway_test", "gateway_1")]
    assert result["switches"]["gateway_1"]["pipeline"]["ok"] is True


def test_int_query_telemetry_tool_forwards_filters(monkeypatch) -> None:
    calls: list[dict] = []
    row = {
        "flow_id": "flow-1",
        "packet_id": "packet-1",
        "hop_sequence": [{"switch_id": 1}],
        "sink_seen": True,
        "trace_complete": True,
    }

    class FakeAPI:
        def __init__(self, lab_name: str) -> None:
            self.lab_name = lab_name

        def int_query_telemetry(self, **kwargs) -> list[dict]:
            calls.append({"lab_name": self.lab_name, **kwargs})
            return [row]

    monkeypatch.setattr(telemetry_server, "get_lab_name", lambda: "gateway_session")
    monkeypatch.setattr(telemetry_server, "KatharaTelemetryAPI", FakeAPI)

    result = telemetry_server.int_query_telemetry(
        "1750000000",
        end_time="1750000010",
        src="192.0.2.11",
        dst="10.0.1.11",
        protocol="6",
        src_port=23456,
        dst_port=80,
        flow_id="flow-1",
        packet_id="packet-1",
        limit=5,
    )
    assert result == [row]
    assert calls == [
        {
            "lab_name": "gateway_session",
            "start_time": "1750000000",
            "end_time": "1750000010",
            "src": "192.0.2.11",
            "dst": "10.0.1.11",
            "protocol": "6",
            "src_port": 23456,
            "dst_port": 80,
            "flow_id": "flow-1",
            "packet_id": "packet-1",
            "limit": 5,
        }
    ]


def test_p4_runtime_state_omits_zero_counters_and_compacts_ports(monkeypatch) -> None:
    api = KatharaBMv2API.__new__(KatharaBMv2API)
    api.lab = type("Lab", (), {"name": "p4_dc_gateway_test"})()
    manager_state = {
        "switches": {
            "gateway_1": {
                "pipeline": {"ok": True},
                "counters": {
                    "ingress": {
                        "0": {"packets": 0, "bytes": 0},
                        "3": {"packets": 4, "bytes": 512},
                    }
                },
                "registers": {"queue_occupancy": {"0": 0, "3": 7}},
                "runtime_config": {"ecn_config": []},
            }
        }
    }
    links = [
        {"ifname": "lo", "operstate": "UNKNOWN", "mtu": 65536},
        {
            "ifname": "eth2",
            "operstate": "UP",
            "mtu": 1500,
            "stats64": {
                "rx": {"packets": 10, "bytes": 1000, "errors": 0, "dropped": 0},
                "tx": {"packets": 8, "bytes": 800, "errors": 0, "dropped": 1},
            },
        },
    ]

    def _exec(_host: str, command: str, **_kwargs) -> str:
        if "p4rt_manager.py" in command:
            return json.dumps(manager_state)
        return json.dumps(links)

    monkeypatch.setattr(api, "exec_cmd", _exec)
    state = api.p4_get_runtime_state("gateway_1")["switches"]["gateway_1"]

    assert state["counters"] == {"ingress": {"3": {"packets": 4, "bytes": 512}}}
    assert state["queue_statistics"] == {"occupancy": {"3": 7}}
    assert state["ports"] == [
        {
            "name": "eth2",
            "state": "UP",
            "mtu": 1500,
            "rx": {"packets": 10, "bytes": 1000, "errors": 0, "dropped": 0},
            "tx": {"packets": 8, "bytes": 800, "errors": 0, "dropped": 1},
        }
    ]


def test_get_tc_statistics_works_with_kathara_base_api(monkeypatch) -> None:
    api = KatharaBMv2API.__new__(KatharaBMv2API)
    calls: list[tuple[str, str]] = []

    def _exec(host_name: str, command: str, **_kwargs) -> str:
        calls.append((host_name, command))
        return "qdisc fq_codel 0: root"

    monkeypatch.setattr(api, "exec_cmd", _exec)
    monkeypatch.setattr(host_server, "get_lab_api", lambda: api)

    assert host_server.get_tc_statistics("gateway_1", "eth2").startswith("qdisc")
    assert calls == [("gateway_1", "tc -s qdisc show dev eth2")]
