from __future__ import annotations

import json
from dataclasses import replace
from nika.problems.rca.inventory import load_offline_net_env

import pytest

from nika.validation.batfish.isis import (
    build_isis_snapshot,
    parse_frr_isis,
    project_frr_isis,
)
from nika.net_env.contract import AdjacencyExpectation, ValidationIntent
from nika.validation.batfish.client import _normalize_isis_interfaces
from nika.validation.batfish.compiler import compile_intent
from nika.validation.batfish.verifier import _evaluate_adjacency

pytestmark = pytest.mark.unit


def _config(node="r1", *, metric=10, loopback_isis=True):
    loopback = " ip router isis NIKA\n isis passive\n" if loopback_isis else ""
    return f"""!
frr version 10.5.3
frr defaults traditional
hostname {node}
!
interface lo
 ip address 10.255.0.1/32
{loopback}!
interface eth0
 ip address 10.0.0.0/31
 ip router isis NIKA
 isis circuit-type level-2-only
 isis metric {metric}
!
router isis NIKA
 net 49.0001.0000.0aff.0001.00
 is-type level-2-only
 metric-style wide
!
log file /var/log/frr/frr.log
!
"""


@pytest.mark.parametrize("process_name", ["CORE", "NIKA"])
def test_parser_retains_net_interface_addresses_cost_and_passive_state(process_name):
    net, interfaces = parse_frr_isis("r1", _config().replace("NIKA", process_name))

    assert net == "49.0001.0000.0aff.0001.00"
    assert str(interfaces["lo"]["address"]) == "10.255.0.1/32"
    assert interfaces["lo"]["metric"] == 10
    assert interfaces["lo"]["passive"] is True
    assert interfaces["lo"]["enabled"] is True
    assert interfaces["eth0"]["metric"] == 10
    assert interfaces["eth0"]["passive"] is False
    assert interfaces["eth0"]["enabled"] is True
    assert str(interfaces["eth0"]["address"]) == "10.0.0.0/31"


def test_parser_rejects_interfaces_bound_to_a_different_process():
    config = _config().replace("ip router isis NIKA", "ip router isis CORE", 1)
    with pytest.raises(ValueError, match="Inconsistent FRR IS-IS process names"):
        parse_frr_isis("r1", config)


def test_parser_and_projection_follow_mutated_metric_and_loopback_withdrawal():
    config = _config(metric=77, loopback_isis=False)
    _, interfaces = parse_frr_isis("r1", config)

    assert interfaces["eth0"]["metric"] == 77
    assert interfaces["lo"]["address"] is not None
    assert interfaces["lo"]["enabled"] is False
    projected = project_frr_isis("r1", config)
    assert "metric 77 level 2" in projected
    assert "interface Loopback0" in projected
    assert "passive" not in projected


@pytest.mark.parametrize(
    "mutation",
    [
        lambda text: text.replace("router isis NIKA", "router ospf"),
        lambda text: text.replace("metric-style wide", "metric-style narrow"),
        lambda text: text.replace("isis metric 10", "isis metric 16777215"),
        lambda text: text.replace(
            " isis metric 10", " isis metric 10\n ipv6 address 2001:db8::1/64"
        ),
        lambda text: text.replace(
            " isis metric 10", " isis metric 10\n isis metric 20"
        ),
    ],
)
def test_parser_rejects_protocols_and_attributes_outside_the_subset(mutation):
    with pytest.raises(ValueError):
        parse_frr_isis("r1", mutation(_config()))


def test_projection_uses_ios_xr_interface_names_and_original_shutdown_endpoint():
    projected = project_frr_isis("r1", _config(), shutdown={("r1", "eth0")})

    assert "interface Loopback0\n ipv4 address 10.255.0.1/32" in projected
    assert (
        "interface GigabitEthernet0/0/0/0\n ipv4 address 10.0.0.0/31\n shutdown"
        in projected
    )
    assert "router isis NIKA\n net 49.0001.0000.0aff.0001.00" in projected
    assert "passive" in projected


def test_snapshot_writes_router_only_edges_and_interface_aliases(tmp_path):
    env = load_offline_net_env(
        "isp_abilene", igp="isis", bgp_mode="none", backend="kathara"
    )
    snapshot = build_isis_snapshot(env, env.validation_contract, tmp_path)
    node = env.plan.nodes[0].device_name
    assert snapshot.metadata["interface_aliases"][node]["Loopback0"] == "lo"
    assert (
        snapshot.metadata["interface_aliases"][node]["GigabitEthernet0/0/0/0"] == "eth0"
    )
    layer1 = json.loads((snapshot.path / "batfish/layer1_topology.json").read_text())
    assert len(layer1["edges"]) == len(env.plan.links)
    assert {p.stem for p in (snapshot.path / "configs").iterdir()} == {
        n.device_name for n in env.plan.nodes
    }


def test_snapshot_rejects_static_route_startup(tmp_path):
    env = load_offline_net_env(
        "isp_abilene", igp="isis", bgp_mode="none", backend="kathara"
    )
    node = env.plan.nodes[0]
    changed = replace(
        node,
        startup_commands=(*node.startup_commands, "ip route add default via 10.0.0.1"),
    )
    env.plan = replace(env.plan, nodes=(changed, *env.plan.nodes[1:]))
    with pytest.raises(ValueError, match="startup command"):
        build_isis_snapshot(env, env.validation_contract, tmp_path)


def _isis_intent() -> ValidationIntent:
    return ValidationIntent(
        id="adj.isis.r1.r2.eth0",
        description="Expected IS-IS link.",
        property="adjacency",
        expected="established",
        adjacency=AdjacencyExpectation(
            protocol="isis",
            isis_level=2,
            local_node="r1",
            remote_node="r2",
            local_address="10.0.0.0",
            remote_address="10.0.0.1",
            local_interface="eth0",
            remote_interface="eth0",
            local_router_id="1.1.1.1",
            remote_router_id="2.2.2.2",
        ),
    )


def test_compiles_isis_adjacency_as_topology_edge_query():
    question = compile_intent(_isis_intent())[0]

    assert question.kind == "isis_adjacency"
    assert question.parameters["local_interface"] == "eth0"
    assert question.parameters["remote_interface"] == "eth0"


def test_batfish_edge_aliases_normalize_to_original_interfaces():
    rows = [
        {
            "Interface": "r1[GigabitEthernet0/0/0/0]",
            "Remote_Interface": "r2[GigabitEthernet0/0/0/0]",
        }
    ]

    normalized = _normalize_isis_interfaces(
        rows,
        {
            "r1": {"GigabitEthernet0/0/0/0": "eth0"},
            "r2": {"GigabitEthernet0/0/0/0": "eth0"},
        },
    )

    assert normalized == [{"Interface": "r1[eth0]", "Remote_Interface": "r2[eth0]"}]


def test_isis_evaluator_matches_parallel_link_by_both_interfaces():
    expectation = _isis_intent().adjacency
    assert expectation is not None
    edges = [
        {"Interface": "r1[eth1]", "Remote_Interface": "r2[eth1]"},
        {"Interface": "r1[eth0]", "Remote_Interface": "r2[eth1]"},
        {"Interface": "r1[eth0]", "Remote_Interface": "r2[eth0]"},
    ]

    passed, _, violations, matched = _evaluate_adjacency(expectation, edges)

    assert passed
    assert matched == [edges[-1]]
    assert violations == []


def test_isis_evaluator_rejects_wrong_parallel_counterpart():
    expectation = _isis_intent().adjacency
    assert expectation is not None
    edges = [
        {"Interface": "r1[eth0]", "Remote_Interface": "r2[eth1]"},
        {"Interface": "r1[eth1]", "Remote_Interface": "r2[eth0]"},
    ]

    passed, _, violations, matched = _evaluate_adjacency(expectation, edges)

    assert not passed
    assert matched == []
    assert violations == edges
