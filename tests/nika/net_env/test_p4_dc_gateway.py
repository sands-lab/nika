"""Contracts for the P4 gateway benchmark scenario."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from benchmark.inject_resolve import resolve_inject_params
from traffic.burst import build_burst_flows, flow_id_for_five_tuple
from nika.net_env.p4_dc_gateway.control import build_gateway_intent
from nika.net_env.p4_dc_gateway.topology_model import (
    CONN_TABLE_CAPACITY,
    VIP_IP,
    VIP_PORT,
    SIZE_TABLE,
    build_gateway_fabric_model,
)
from nika.net_env.p4_dc_gateway.verify import (
    _collector_telemetry,
    _emit_int_probe,
    _p4runtime_consistent,
)
from nika.net_env.net_env_pool import list_all_net_envs
from nika.service.kathara.bmv2_api import KatharaBMv2API
from nika.service.kathara.telemetry_api import KatharaTelemetryAPI


@pytest.mark.parametrize("size", ["s", "m", "l"])
def test_gateway_inventory_and_full_mesh(size: str) -> None:
    model = build_gateway_fabric_model(size)  # type: ignore[arg-type]
    dimensions = (
        model.gateway_count,
        model.spine_count,
        model.leaf_count,
        model.client_count,
        model.service_count,
    )
    assert dimensions == SIZE_TABLE[size]
    assert len(model.gateway_spine_links) == model.gateway_count * model.spine_count
    assert len(model.spine_leaf_links) == model.spine_count * model.leaf_count
    assert all(len(model.services_on_leaf(leaf)) == 2 for leaf in model.leaves)
    assert all(model.client_on_gateway(gateway) for gateway in model.gateways)


def test_gateway_intent_has_roles_ecmp_telemetry_and_ecn() -> None:
    model = build_gateway_fabric_model("s")
    intent = build_gateway_intent(model)
    assert intent["pipeline"]["ecmp_hash"] == "5-tuple"
    for name, switch in intent["switches"].items():
        assert switch["role"] == model.switch_info[name].role
        assert switch["ecn"]
        assert switch["telemetry_ip"].startswith("172.29.0.")
        assert switch["members"] and switch["groups"] and switch["ipv4_lpm"]
    assert all(
        len(group["member_ids"]) == model.spine_count
        for gateway in model.gateways
        for group in intent["switches"][gateway]["groups"]
        if group["kind"] == "service_ecmp"
    )
    lb = intent["l4_load_balancer"]
    assert lb["vip"] == {"ip": VIP_IP, "port": VIP_PORT, "protocol": "tcp"}
    assert lb["conn_table"]["capacity"] == CONN_TABLE_CAPACITY
    assert len(lb["backends"]) == 2
    assert all(
        "l4_load_balancer" in intent["switches"][gateway] for gateway in model.gateways
    )


def test_public_registry_contains_gateway() -> None:
    public = list_all_net_envs()
    assert "p4_dc_gateway" in public


@pytest.mark.parametrize(
    "problem",
    [
        "silent_egress_packet_loss",
        "int_insufficient_mtu_headroom",
        "p4_ecn_threshold_misconfiguration",
        "p4_tcam_entry_corruption",
        "tcp_syn_flood_attack",
    ],
)
def test_gateway_inject_resolution_is_seed_stable(problem: str) -> None:
    first = resolve_inject_params(problem, "p4_dc_gateway", "s", seed=91)
    second = resolve_inject_params(problem, "p4_dc_gateway", "s", seed=91)
    assert first == second


def test_burst_flow_identity_is_seed_stable() -> None:
    first = build_burst_flows(["client_1", "client_2"], "service_1_1", "tcp", 7)
    second = build_burst_flows(["client_1", "client_2"], "service_1_1", "tcp", 7)
    assert first == second
    assert len({flow.flow_id for flow in first}) == 2
    assert len({flow.source_port for flow in first}) == 2


def test_burst_supports_deterministic_flow_fanout() -> None:
    flows = build_burst_flows(["client_1"], "service_1_1", "tcp", 7, 4)
    assert len(flows) == 4
    assert len({flow.source_port for flow in flows}) == 4


def test_burst_and_telemetry_share_five_tuple_flow_id() -> None:
    assert flow_id_for_five_tuple("192.0.2.10", "10.0.1.11", "tcp", 23456, 5201) == (
        "1367f06366f44e97"
    )


def test_telemetry_query_aligns_unix_second_event_times(monkeypatch) -> None:
    api = KatharaTelemetryAPI.__new__(KatharaTelemetryAPI)
    api.lab = SimpleNamespace(name="p4_dc_gateway_a1b2c3")
    row = {
        "packet_timestamp": 1_750_000_000_500_000_000,
        "flow_id": "flow-a",
        "hop_sequence": [],
    }
    monkeypatch.setattr(
        api, "exec_cmd", lambda *_args, **_kwargs: __import__("json").dumps(row)
    )
    assert api.int_query_telemetry(
        "1750000000", end_time="1750000001", flow_id="flow-a"
    ) == [row]


def test_agent_bmv2_api_hides_private_fault_state(monkeypatch) -> None:
    api = KatharaBMv2API.__new__(KatharaBMv2API)
    api.lab = SimpleNamespace(name="p4_dc_gateway_a1b2c3")
    manager_state = {
        "switches": {
            "gateway_1": {
                "pipeline": {"ok": True},
                "registers": {
                    "queue_occupancy": {"1": 4},
                    "internal_fault_loss_threshold": {"1": 2},
                },
                "runtime_config": {"ecn_config": []},
            }
        }
    }

    def _exec(_host: str, command: str, **_kwargs) -> str:
        if "p4rt_manager.py" in command:
            return __import__("json").dumps(manager_state)
        return "1: lo: <LOOPBACK>"

    monkeypatch.setattr(api, "exec_cmd", _exec)
    state = api.p4_get_runtime_state("gateway_1")
    assert "internal_fault" not in __import__("json").dumps(state)


def test_p4_mcp_servers_expose_only_live_interfaces() -> None:
    from nika.service.mcp_server.kathara import bmv2_server, telemetry_server

    assert set(bmv2_server.mcp._tool_manager._tools) == {"p4_get_runtime_state"}
    assert set(telemetry_server.mcp._tool_manager._tools) == {"int_query_telemetry"}


class _CollectorRuntime:
    def __init__(self, records: list[dict]) -> None:
        self.output = "\n".join(__import__("json").dumps(row) for row in records)

    def exec(self, _host: str, _command: str, timeout: float = 10.0) -> str:
        return self.output


def _hop(switch_id: int) -> dict:
    return {
        "switch_id": switch_id,
        "ingress_port": 1,
        "egress_port": 2,
        "ingress_timestamp": 100,
        "egress_timestamp": 120,
        "hop_latency": 20,
        "queue_occupancy": 0,
        "ecn": 0,
        "m": 0,
        "e": 0,
    }


def test_collector_verification_requires_complete_expected_trace() -> None:
    model = build_gateway_fabric_model("s")
    ok, details = _collector_telemetry(_CollectorRuntime([]), model)
    assert not ok
    assert details["candidate_records"] == 0

    record = {
        "flow_id": "flow-1",
        "packet_id": "packet-1",
        "packet_timestamp": 123,
        "src": model.clients[0].ip,
        "dst": model.services[0].ip,
        "protocol": 6,
        "dst_port": 80,
        "sink_seen": True,
        "trace_complete": True,
        "hop_sequence": [
            _hop(model.switch_info[model.gateways[0]].device_id),
            _hop(model.switch_info[model.spines[0]].device_id),
            _hop(model.switch_info[model.leaves[0]].device_id),
        ],
    }
    ok, details = _collector_telemetry(_CollectorRuntime([record]), model)
    assert ok
    assert details["roles_seen"] == {
        "gateway": True,
        "spine": True,
        "leaf": True,
    }

    record["hop_sequence"] = record["hop_sequence"][:2]
    ok, details = _collector_telemetry(_CollectorRuntime([record]), model)
    assert not ok
    assert not details["roles_seen"]["leaf"]


def test_int_probe_uses_service_address_instead_of_vip() -> None:
    model = build_gateway_fabric_model("s")

    class Runtime:
        def __init__(self) -> None:
            self.command = ""

        def exec(self, _host: str, command: str, timeout: float = 10.0) -> str:
            self.command = command
            return ""

    runtime = Runtime()
    _emit_int_probe(runtime, model)
    assert model.services[0].ip in runtime.command
    assert VIP_IP not in runtime.command


def test_gateway_p4runtime_verification_checks_programmed_state() -> None:
    model = build_gateway_fabric_model("s")
    live = {
        name: {
            "pipeline": {"ok": True},
            "mismatches": [],
            "ipv4_lpm": [{}],
            "groups": [{}],
            "members": [{}],
        }
        for name in model.fabric_switches()
    }
    ok, _details = _p4runtime_consistent({"ok": True, "switches": live}, model)
    assert ok

    live[model.gateways[0]]["mismatches"] = ["ipv4_lpm"]
    ok, _details = _p4runtime_consistent({"ok": True, "switches": live}, model)
    assert not ok
