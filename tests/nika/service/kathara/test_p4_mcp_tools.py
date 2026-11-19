"""Contracts for the P4Runtime and INT-MD MCP tools."""

from __future__ import annotations

import json
import re

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

    assert set(bmv2_tools) == {"p4rt_exec", "lb_get_state", "bmv2_get_ports"}
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
        "offset",
        "view",
        "egress_switch",
        "egress_port",
        "downstream_switch",
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
    assert result == {"records": [row], "offset": 0, "next_offset": 1}
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
            "offset": 0,
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


def test_p4rt_exec_runs_queries(monkeypatch) -> None:
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
        "delete-lpm --switch leaf_1 --prefix 10.0.0.0/8",
        "insert --switch leaf_1 --table int_watchlist --match 'srcAddr=10.0.1.11/32' --match 'dstPort=80&&&65535' --action watch_int --priority 10",
        "modify --switch gateway_1 --table ecn_config --match egress_port=1 --action set_ecn_threshold --param threshold=64",
        "delete --switch leaf_1 --table int_watchlist --priority 10",
        "--p4info /tmp/custom read",
        "read --switch 'leaf_1;id'",
    ],
)
def test_p4rt_exec_passes_all_manager_arguments_quoted(monkeypatch, args):
    import shlex

    api, calls = _recording_api(monkeypatch)
    assert json.loads(api.p4rt_exec(args))["ok"]
    command = calls[0][1].split("p4rt_manager.py ", 1)[1]
    assert shlex.split(command) == shlex.split(args)
    assert calls[0][0] == "fabric_mgr"


def test_p4rt_exec_reports_malformed_quotes(monkeypatch):
    api, calls = _recording_api(monkeypatch)
    assert api.p4rt_exec("read --switch '").startswith("error:")
    assert calls == []


def test_p4rt_exec_help_describes_writes(monkeypatch):
    api, calls = _recording_api(monkeypatch)
    assert "insert|modify|delete" in api.p4rt_exec("")
    assert calls == []


def test_exec_shell_reads_tc_statistics(monkeypatch) -> None:
    api = KatharaBMv2API.__new__(KatharaBMv2API)
    calls: list[tuple[str, str]] = []

    def _exec(host_name: str, command: str, **_kwargs) -> str:
        calls.append((host_name, command))
        return "qdisc fq_codel 0: root"

    monkeypatch.setattr(api, "exec_cmd", _exec)
    monkeypatch.setattr(host_server, "get_lab_api", lambda: api)
    monkeypatch.setattr("nika.mcp.session_context.require_session_id", lambda: "test")
    monkeypatch.setattr(
        "nika.mcp.gateway.access.node_roles_for_session",
        lambda sid: {"client_1": "host"},
    )

    assert host_server.exec_shell("client_1", "tc -s qdisc show dev eth0").startswith(
        "qdisc"
    )
    assert calls == [("client_1", "tc -s qdisc show dev eth0")]


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


def test_int_delivery_correlates_hops_and_preserves_missing_packets(monkeypatch):
    from nika.service.kathara.telemetry_api import collapse_traces

    rows = [
        {
            "flow_id": "a",
            "packet_id": "1",
            "packet_timestamp": 1,
            "sink_seen": False,
            "trace_complete": False,
            "hop_sequence": [
                {"switch": "gateway_1", "egress_port": 1, "queue_occupancy": 0}
            ],
        },
        {
            "flow_id": "a",
            "packet_id": "1",
            "packet_timestamp": 2,
            "sink_seen": True,
            "trace_complete": True,
            "hop_sequence": [
                {"switch": "gateway_1", "egress_port": 1, "queue_occupancy": 0},
                {"switch": "leaf_1", "egress_port": 3, "queue_occupancy": 0},
            ],
        },
        {
            "flow_id": "a",
            "packet_id": "2",
            "packet_timestamp": 3,
            "sink_seen": False,
            "trace_complete": False,
            "hop_sequence": [
                {"switch": "gateway_1", "egress_port": 1, "queue_occupancy": 0}
            ],
        },
    ]
    mature = collapse_traces(rows, 4_000_000_000)
    assert len(mature) == 2
    assert sum(row["sink_seen"] for row in mature) == 1
    assert len(collapse_traces(rows, 100)) == 1
    # Late intermediate reports cannot overwrite an observed sink delivery.
    assert collapse_traces(rows + [rows[0]], 4_000_000_000) == mature

    class API:
        def __init__(self, **kwargs):
            pass

        def int_query_telemetry(self, **kwargs):
            return mature

    monkeypatch.setattr(telemetry_server, "KatharaTelemetryAPI", API)
    monkeypatch.setattr(telemetry_server, "get_lab_name", lambda: "lab")
    result = telemetry_server.int_query_telemetry(
        "0", view="delivery", egress_switch="gateway_1", egress_port=1
    )
    assert (result["observed"], result["delivered"], result["missing"]) == (2, 1, 1)


def test_downstream_ingress_proves_wire_delivery_before_queue_drop(monkeypatch):
    from nika.service.kathara.telemetry_api import collapse_traces

    source = {
        "flow_id": "a",
        "packet_id": "p",
        "packet_timestamp": 1,
        "sink_seen": False,
        "trace_complete": False,
        "hop_sequence": [{"switch": "spine_1", "egress_port": 3, "queue_occupancy": 1}],
    }
    ingress = {**source, "packet_timestamp": 2, "arrival_switches": ["leaf_1"]}
    mature = collapse_traces([source, ingress], 5_000_000_000)
    assert mature[0]["arrival_switches"] == ["leaf_1"]
    assert not mature[0]["sink_seen"]

    class API:
        def __init__(self, **kwargs):
            pass

        def int_query_telemetry(self, **kwargs):
            return mature

    monkeypatch.setattr(telemetry_server, "KatharaTelemetryAPI", API)
    monkeypatch.setattr(telemetry_server, "get_lab_name", lambda: "lab")
    result = telemetry_server.int_query_telemetry(
        "0",
        view="delivery",
        egress_switch="spine_1",
        egress_port=3,
        downstream_switch="leaf_1",
    )
    assert result["delivered"] == 1 and result["missing"] == 0


@pytest.mark.parametrize(
    ("response", "error"),
    [
        ('[{"ifname":"eth0","operstate":"UP","mtu":1500}]\n__rc=0\n', None),
        (
            "[TIMEOUT] ip command exceeded 5s",
            "Observation on spine_3 returned no exit status: [TIMEOUT]",
        ),
        (
            "permission denied\n__rc=1\n",
            "Observation on spine_3 failed (rc=1): permission denied",
        ),
        ("not JSON\n__rc=0\n", "Invalid interface-state JSON from spine_3"),
    ],
)
def test_bmv2_ports_preserve_observation_failures(monkeypatch, response, error):
    from types import SimpleNamespace

    from nika.mcp import session_context
    from nika.problems.rca import inventory

    env = SimpleNamespace(
        bmv2_switches=["spine_3"],
        model=SimpleNamespace(
            ports={"spine_3": [SimpleNamespace(name="eth0", bmv2_port=1)]}
        ),
    )
    runtime = SimpleNamespace(exec=lambda *args, **kwargs: response)
    monkeypatch.setattr(session_context, "get_lab_runtime", lambda: runtime)
    monkeypatch.setattr(session_context, "get_session_meta", lambda: {})
    monkeypatch.setattr(inventory, "load_session_offline_net_env", lambda meta: env)
    if error is not None:
        with pytest.raises(RuntimeError, match=re.escape(error)):
            bmv2_server.bmv2_get_ports("spine_3")
    else:
        result = json.loads(bmv2_server.bmv2_get_ports("spine_3"))
        assert result["ports"][0]["interface"] == "eth0"
        assert result["ports"][0]["state"]["operstate"] == "UP"
        assert result["ports"][0]["state"]["mtu"] == 1500
