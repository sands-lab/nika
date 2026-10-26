"""Contracts for the P4Runtime and INT-MX MCP tools."""

from __future__ import annotations

import json

import pytest

from nika.mcp.servers.kathara import bmv2_server, telemetry_server
from nika.service.kathara.bmv2_api import KatharaBMv2API
from nika.mcp.servers.common import host_server

pytestmark = pytest.mark.unit


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


def _recording_api(monkeypatch) -> tuple[KatharaBMv2API, list[tuple[str, str]]]:
    api = KatharaBMv2API.__new__(KatharaBMv2API)
    calls: list[tuple[str, str]] = []

    def _exec(host: str, command: str, **_kwargs) -> str:
        calls.append((host, command))
        return json.dumps({"ok": True, "switches": {}})

    monkeypatch.setattr(api, "exec_cmd", _exec)
    return api, calls


def test_p4rt_exec_runs_read_only_queries(monkeypatch) -> None:
    api, calls = _recording_api(monkeypatch)
    json.loads(api.p4rt_exec("read --switch gateway_1"))
    api.p4rt_exec("read-table --switch leaf_1 --table ipv4_lpm")
    api.p4rt_exec("counters")
    assert [cmd.split("p4rt_manager.py ", 1)[1] for _host, cmd in calls] == [
        "read --switch gateway_1",
        "read-table --switch leaf_1 --table ipv4_lpm",
        "counters",
    ]
    assert {host for host, _cmd in calls} == {"fabric_mgr"}


@pytest.mark.parametrize(
    "args",
    [
        "apply",
        "apply --switch leaf_1",
        "write --switch leaf_1",
        "read --intent /tmp/other.json",
        "--p4info /tmp/x read",
        "read --switch leaf_1 --json /tmp/x",
        "read @/tmp/args",
        "read --switch 'leaf_1;id'",
        "read-table --switch leaf_1",
    ],
)
def test_p4rt_exec_rejects_writes_and_overrides(monkeypatch, args: str) -> None:
    api, calls = _recording_api(monkeypatch)
    assert api.p4rt_exec(args).startswith("error:")
    assert calls == []


def test_p4rt_exec_help_lists_only_read_commands(monkeypatch) -> None:
    api, calls = _recording_api(monkeypatch)
    text = api.p4rt_exec("--help")
    assert calls == []
    assert {"read", "read-table", "counters"} <= set(text.split())
    for word in ("apply", "write", "--kind", "--intent", "--p4info", "--json"):
        assert word not in text.split()


def test_exec_shell_reads_tc_statistics(monkeypatch) -> None:
    api = KatharaBMv2API.__new__(KatharaBMv2API)
    calls: list[tuple[str, str]] = []

    def _exec(host_name: str, command: str, **_kwargs) -> str:
        calls.append((host_name, command))
        return "qdisc fq_codel 0: root"

    monkeypatch.setattr(api, "exec_cmd", _exec)
    monkeypatch.setattr(host_server, "get_lab_api", lambda: api)

    assert host_server.exec_shell("gateway_1", "tc -s qdisc show dev eth2").startswith(
        "qdisc"
    )
    assert calls == [("gateway_1", "tc -s qdisc show dev eth2")]


def test_collapse_traces_keeps_latest_and_hides_in_flight() -> None:
    from nika.service.kathara.telemetry_api import collapse_traces

    def rec(pid: str, ts: int, complete: bool) -> dict:
        return {
            "flow_id": "f",
            "packet_id": pid,
            "packet_timestamp": ts,
            "trace_complete": complete,
        }

    now = 10_000_000_000
    rows = collapse_traces(
        [
            rec("a", 1, False),
            rec("a", 2, True),  # completed: only this one survives
            rec("b", now - 100, False),  # in flight: hidden
            rec("c", now - 5_000_000_000, False),  # stale partial: reported
        ],
        now,
    )
    assert [(r["packet_id"], r["trace_complete"]) for r in rows] == [
        ("a", True),
        ("c", False),
    ]
