"""Forwarding regressions retain concrete parallel edges and all ECMP branches."""

from dataclasses import replace
from ipaddress import IPv4Network

import pytest

from nika.net_env.isp.fib import FibHop, FibRoute, linux_fib, srl_fib, verify_fib
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.topology.models import NetworkTopology, TopoLink, TopoNode

pytestmark = pytest.mark.unit


def _network():
    topology = NetworkTopology(
        name="parallel",
        source_format="sndlib-xml",
        meta={},
        nodes=tuple(TopoNode(id=node) for node in ("A", "B", "C")),
        links=(
            TopoLink(id="ab-cheap", source="A", target="B", routing_cost=1),
            TopoLink(id="ab-costly", source="A", target="B", routing_cost=9),
            TopoLink(id="bc", source="B", target="C", routing_cost=1),
        ),
        demands=(),
    )
    plan = compile_isp_plan(
        IspConfig(topology="polska", metric_strategy="routing_cost"), topology=topology
    )
    fibs = {}
    for node in plan.nodes:
        routes = [FibRoute(IPv4Network(node.loopback + "/32"), local=True)]
        for target in plan.nodes:
            if target == node:
                continue
            peer = "b" if node.device_name != "b" else target.device_name
            interface = min(
                (i for i in node.interfaces if i.peer_device == peer),
                key=lambda i: i.metric,
            )
            routes.append(
                FibRoute(
                    IPv4Network(target.loopback + "/32"),
                    (FibHop(interface.name, interface.peer_address),),
                )
            )
        fibs[node.device_name] = tuple(routes)
    return plan, fibs


def test_all_pairs_use_metric_shortest_paths():
    plan, fibs = _network()
    result = verify_fib(plan, fibs)
    assert result["pairs_checked"] == 6
    assert result["all_pairs_reachable"] and result["shortest_paths"]


def test_costlier_parallel_member_is_detected_even_with_same_peer():
    plan, fibs = _network()
    source = plan.nodes[0]
    costly = max(
        (i for i in source.interfaces if i.peer_device == "b"), key=lambda i: i.metric
    )
    routes = list(fibs[source.device_name])
    routes[1] = replace(routes[1], hops=(FibHop(costly.name, costly.peer_address),))
    fibs[source.device_name] = tuple(routes)
    result = verify_fib(plan, fibs)
    assert result["all_pairs_reachable"]
    assert not result["shortest_paths"]
    assert any(
        row["reason"] == "non_shortest_exit" and row["link_id"] == costly.link_id
        for row in result["failures"]
    )


def test_one_bad_ecmp_branch_cannot_be_hidden_by_a_good_branch():
    plan, fibs = _network()
    route = fibs["a"][1]
    fibs["a"] = (
        fibs["a"][0],
        replace(route, hops=route.hops + (FibHop("unknown", "192.0.2.1"),)),
        *fibs["a"][2:],
    )
    result = verify_fib(plan, fibs)
    assert not result["all_pairs_reachable"]
    assert any(row["reason"] == "unknown_igp_exit" for row in result["failures"])


def test_forwarding_loop_is_a_failure_and_refresh_recovers():
    plan, healthy = _network()
    broken = dict(healthy)
    interface = next(i for i in plan.nodes[1].interfaces if i.peer_device == "a")
    target = IPv4Network(plan.nodes[-1].loopback + "/32")
    broken["b"] = tuple(
        replace(route, hops=(FibHop(interface.name, interface.peer_address),))
        if route.prefix == target
        else route
        for route in broken["b"]
    )
    assert not verify_fib(plan, broken)["all_pairs_reachable"]
    assert verify_fib(plan, healthy)["shortest_paths"]


def test_longest_prefix_discard_overrides_a_reachable_route():
    plan, fibs = _network()
    fibs["a"] = (
        fibs["a"][0],
        FibRoute(IPv4Network("10.255.0.0/24"), fibs["a"][1].hops),
        FibRoute(IPv4Network(plan.nodes[-1].loopback + "/32"), discard=True),
    )
    assert not verify_fib(plan, fibs)["all_pairs_reachable"]


def test_linux_normalization_keeps_local_delivery_and_ecmp_members():
    routes = linux_fib(
        [
            {"dst": "10.0.0.1", "type": "local", "table": "local"},
            {
                "dst": "10.0.0.2",
                "nexthops": [
                    {"dev": "eth0", "gateway": "192.0.2.1"},
                    {"dev": "eth1", "gateway": "192.0.2.3"},
                ],
            },
        ]
    )
    assert routes[0].local
    assert len(routes[1].hops) == 2
    with pytest.raises(ValueError, match="unresolved"):
        linux_fib([{"dst": "10.0.0.2", "nhid": 7}])


def test_srl_normalization_uses_programmed_groups_and_resolves_bgp_recursion():
    programmed = {
        "last-successful-operation-type": "add",
        "pending-operation-type": "none",
    }
    payload = {
        "network-instance": [
            {
                "name": "default",
                "route-table": {
                    "ipv4-unicast": {
                        "route": [
                            {
                                "ipv4-prefix": prefix,
                                "active": True,
                                "next-hop-group": group,
                                "fib-programming": programmed,
                            }
                            for prefix, group in (
                                ("10.255.0.1/32", "local"),
                                ("10.255.0.9/32", "direct"),
                                ("203.0.113.0/24", "recursive"),
                            )
                        ]
                    },
                    "next-hop-group": [
                        {
                            "index": group,
                            "fib-programming": programmed.copy(),
                            "next-hop": [{"next-hop": hop}],
                        }
                        for group, hop in (
                            ("local", "extract"),
                            ("direct", "interface"),
                            ("recursive", "indirect"),
                        )
                    ],
                    "next-hop": [
                        {"index": "extract", "type": "extract"},
                        {
                            "index": "interface",
                            "type": "direct",
                            "ip-address": "10.0.0.9",
                            "subinterface": "ethernet-1/2.0",
                        },
                        {
                            "index": "indirect",
                            "type": "indirect",
                            "resolving-route": {"next-hop-group": "direct"},
                        },
                    ],
                },
            }
        ]
    }
    routes = {str(route.prefix): route for route in srl_fib(payload)}
    assert routes["10.255.0.1/32"].local
    assert routes["10.255.0.9/32"].hops
    assert routes["203.0.113.0/24"].hops == routes["10.255.0.9/32"].hops
    group = payload["network-instance"][0]["route-table"]["next-hop-group"][0]
    group["fib-programming"]["pending-operation-type"] = "modify"
    with pytest.raises(ValueError, match="pending"):
        srl_fib(payload)
    group["fib-programming"].update(
        {
            "pending-operation-type": "none",
            "last-failed-operation-type": "add",
            "last-failed-operation-timestamp": "2026-10-15T12:00:00Z",
            "last-successful-operation-timestamp": "2026-10-15T12:01:00Z",
        }
    )
    assert srl_fib(payload)
    group["fib-programming"]["last-failed-operation-timestamp"] = "2026-10-15T12:02:00Z"
    with pytest.raises(ValueError, match="latest FIB programming operation failed"):
        srl_fib(payload)


@pytest.mark.parametrize("reverse", [False, True])
def test_linux_route_priority_and_local_table_precede_other_candidates(reverse):
    from nika.net_env.isp.fib import _RouteIndex

    candidates = [
        {"dst": "10.255.0.2", "dev": "eth0", "gateway": "192.0.2.1", "metric": 20},
        {"dst": "10.255.0.2", "type": "blackhole", "metric": 1},
    ]
    routes = linux_fib(list(reversed(candidates)) if reverse else candidates)
    assert _RouteIndex(routes).lookup("10.255.0.2").discard
    local = FibRoute(IPv4Network("10.255.0.0/24"), local=True, table="local")
    assert _RouteIndex((*routes, local)).lookup("10.255.0.2").local
