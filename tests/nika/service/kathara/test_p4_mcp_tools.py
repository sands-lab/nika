"""Contracts for the P4Runtime and INT-MX MCP tools."""

from __future__ import annotations

import json

import pytest

from nika.mcp.servers.kathara import bmv2_server, telemetry_server
from nika.service.kathara.bmv2_api import KatharaBMv2API, _sanitize_p4rt_payload
from nika.mcp.servers.common import host_server


@pytest.mark.asyncio
async def test_p4_mcp_tool_schemas() -> None:
    bmv2_tools = {tool.name: tool for tool in await bmv2_server.mcp.list_tools()}
    telemetry_tools = {
        tool.name: tool for tool in await telemetry_server.mcp.list_tools()
    }

    assert set(bmv2_tools) == {"p4rt_exec"}
    assert set(bmv2_tools["p4rt_exec"].inputSchema["properties"]) == {"args"}
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


def test_p4rt_exec_tool_calls_session_api(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeAPI:
        def __init__(self, lab_name: str) -> None:
            self.lab_name = lab_name

        def p4rt_exec(self, args: str) -> str:
            calls.append((self.lab_name, args))
            return json.dumps({"switches": {"gateway_1": {"pipeline": {"ok": True}}}})

    monkeypatch.setattr(bmv2_server, "get_lab_name", lambda: "p4_dc_gateway_test")
    monkeypatch.setattr(bmv2_server, "KatharaBMv2API", FakeAPI)

    result = json.loads(bmv2_server.p4rt_exec("read --switch gateway_1"))
    assert calls == [("p4_dc_gateway_test", "read --switch gateway_1")]
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


def test_p4rt_exec_sanitizes_private_fault_registers(monkeypatch) -> None:
    api = KatharaBMv2API.__new__(KatharaBMv2API)
    manager_state = {
        "switches": {
            "gateway_1": {
                "pipeline": {"ok": True},
                "registers": {
                    "queue_occupancy": {"1": 4},
                    "internal_fault_loss_threshold": {"1": 2},
                },
            }
        }
    }

    def _exec(_host: str, command: str, **_kwargs) -> str:
        assert "p4rt_manager.py" in command
        assert "read --switch gateway_1" in command
        return json.dumps(manager_state)

    monkeypatch.setattr(api, "exec_cmd", _exec)
    payload = json.loads(api.p4rt_exec("read --switch gateway_1"))
    assert "internal_fault" not in json.dumps(payload)
    assert payload["switches"]["gateway_1"]["registers"]["queue_occupancy"]["1"] == 4


def test_sanitize_p4rt_payload_drops_internal_fault_keys() -> None:
    cleaned = _sanitize_p4rt_payload(
        {"ok": True, "internal_fault_x": 1, "nested": {"internal_fault_y": 2, "z": 3}}
    )
    assert cleaned == {"ok": True, "nested": {"z": 3}}


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
