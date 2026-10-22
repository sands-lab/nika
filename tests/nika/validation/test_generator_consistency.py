"""Native renderers and netlab must yield the same routing facts for one design."""

from importlib.util import find_spec

import pytest

from nika.validation.consistency import (
    CANARY_PREFIX,
    diff_facts,
    policy_outcome,
    probe_routes,
    resolve_fib,
)


def _route(node, network, *, ip=None, interface=None, protocol="ibgp"):
    return {
        "Node": node,
        "VRF": "default",
        "Network": network,
        "Protocol": protocol,
        "Next_Hop_IP": ip or "AUTO/NONE(-1l)",
        "Next_Hop_Interface": interface or "dynamic",
    }


@pytest.mark.unit
def test_fib_resolves_recursive_bgp_next_hops_through_logical_interfaces():
    rows = [
        _route("r1", "198.51.100.0/24", ip="10.255.0.2"),
        _route("r1", "10.255.0.2/32", ip="10.0.0.1", interface="e1", protocol="ospf"),
        _route("r1", "10.255.0.2/32", ip="10.0.0.3", interface="e2", protocol="ospf"),
        _route("r1", "10.0.0.0/31", interface="e1", protocol="connected"),
        _route("r1", "192.0.2.1/32", interface="null_interface", protocol="static"),
    ]
    facts = resolve_fib(rows, {"r1": {"e1": "eth0", "e2": "eth1"}})
    assert facts["fib", "r1", "default 198.51.100.0/24"] == [
        ["eth0", "10.0.0.1"],
        ["eth1", "10.0.0.3"],
    ]
    assert facts["fib", "r1", "default 10.0.0.0/31"] == [["eth0", None]]
    assert facts["fib", "r1", "default 192.0.2.1/32"] == [["null", None]]


@pytest.mark.unit
def test_policy_outcomes_compare_absent_and_permissive_policies_equally():
    source = {
        "network": "198.51.100.0/24",
        "asPath": [{"asns": [65002], "confederation": False}],
        "communities": [],
        "localPreference": 100,
        "metric": 0,
        "nextHopIp": "10.0.0.5",
        "weight": 0,
    }
    permitted = {"Action": "PERMIT", "Output_Route": dict(source, nextHopIp=None)}
    assert policy_outcome(None, source) == policy_outcome(permitted, source)
    assert policy_outcome({"Action": "DENY"}, source) == {"action": "DENY"}
    rewritten = dict(source, localPreference=200, nextHopIp="192.0.2.1")
    outcome = policy_outcome({"Action": "PERMIT", "Output_Route": rewritten}, source)
    assert (outcome["local_pref"], outcome["next_hop"]) == (200, "192.0.2.1")


@pytest.mark.unit
def test_probe_routes_are_shared_by_both_candidates_and_include_a_canary():
    native = [{"Network": "198.51.100.0/24", "AS_Path": "65001", "Communities": []}]
    candidate = [{"Network": "198.51.100.0/24", "AS_Path": "65002 65001"}]
    probes = probe_routes([native, candidate], {"203.0.113.0/24"}, ("65535:666",))
    assert ("198.51.100.0/24", (65001,), ()) in probes
    assert ("198.51.100.0/24", (65002, 65001), ()) in probes
    assert ("203.0.113.0/24", (), ("65535:666",)) in probes
    assert (CANARY_PREFIX, (), ()) in probes


@pytest.mark.unit
def test_differences_name_the_node_fact_and_both_values():
    native = {("bgp_policy", "r1", "default 10.0.0.1 out x"): {"action": "PERMIT"}}
    candidate = {
        ("bgp_policy", "r1", "default 10.0.0.1 out x"): {"action": "DENY"},
        ("rib", "r2", "default 192.0.2.0/24"): [],
    }
    first, second = diff_facts(native, candidate)
    assert (first.kind, first.node, first.native, first.candidate) == (
        "bgp_policy",
        "r1",
        {"action": "PERMIT"},
        {"action": "DENY"},
    )
    assert second.native is None and second.candidate == []
    assert diff_facts(native, dict(native)) == ()


@pytest.mark.contract
def test_netlab_frr_ebgp_keeps_native_intra_as_next_hop_and_export_scope():
    from nika.net_env.isp.netlab import _bind_ibgp_policies, netlab_topology
    from nika.net_env.net_env_pool import get_net_env_instance

    env = get_net_env_instance(
        "isp_abilene", igp="ospf", bgp_mode="ebgp", backend="kathara"
    )
    assert netlab_topology(env.plan, env.bgp_plan, "frr")["bgp"]["next_hop_self"]
    rr = get_net_env_instance(
        "isp_abilene", igp="ospf", bgp_mode="ibgp_rr", backend="kathara"
    )
    assert not netlab_topology(rr.plan, rr.bgp_plan, "frr")["bgp"]["next_hop_self"]

    config = "route-map BGP-IN-ipv4 permit 10\nroute-map BGP-OUT-ipv4 permit 10\n"
    model = {
        "nodes": {
            "r1": {
                "device": "frr",
                "bgp": {
                    "as": 65001,
                    "neighbors": [{"ipv4": "10.255.0.2", "type": "ibgp"}],
                },
            }
        }
    }
    ebgp = _bind_ibgp_policies({"r1": config}, model, "ebgp")["r1"]
    assert "neighbor 10.255.0.2 route-map BGP-IN-ipv4 in" in ebgp
    assert "route-map BGP-OUT-ipv4 out" not in ebgp
    rr_config = _bind_ibgp_policies({"r1": config}, model, "ibgp_rr")["r1"]
    assert "neighbor 10.255.0.2 route-map BGP-OUT-ipv4 out" in rr_config


@pytest.mark.integration
@pytest.mark.live
@pytest.mark.skipif(find_spec("netsim") is None, reason="Install the netlab extra")
@pytest.mark.parametrize(
    "scenario,topo_size,deployment",
    [
        (
            "isp_abilene_ebgp_rpki",
            None,
            {"backend": "kathara", "device_profile": "frr"},
        ),
        ("dc_clos", "s", {}),
    ],
)
def test_native_and_netlab_candidates_have_equal_routing_facts(
    scenario, topo_size, deployment, tmp_path, batfish_verifier
):
    from nika.workflows.validation.consistency import compare_config_generators

    client = batfish_verifier._client._session
    report = compare_config_generators(
        scenario,
        tmp_path,
        topo_size=topo_size,
        deployment=deployment,
        batfish_host=client.host,
        batfish_port=client.port_v2,
    )
    counts = report.fact_counts["native"]
    assert counts["rib"] and counts["fib"] and counts["bgp_session"]
    assert counts["bgp_policy"] and counts["interface"]
    assert report.consistent, report.summary()
    assert (tmp_path / "report.json").exists()
