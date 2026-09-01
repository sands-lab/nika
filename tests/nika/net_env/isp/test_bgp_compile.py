"""Unit tests for ISP BGP presets (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.isp.bgp import (
    BgpConfigError,
    compile_bgp_plan,
    merge_frr_conf,
    render_bgp_frr_fragment,
    scope_igp_to_bgp_as,
)
from nika.net_env.isp.contract import build_isp_validation_contract
from nika.net_env.isp.igp import (
    IspConfig,
    active_igp_links,
    compile_isp_plan,
    igp_components,
)
from nika.net_env.isp.traffic import (
    TrafficInterval,
    TrafficMatrixSeries,
    attach_traffic_stubs,
)
from nika.topology import list_sndlib_topologies
from nika.topology.models import NetworkTopology, TopoLink, TopoNode


def _isp_plan(name: str = "tiny"):
    return compile_isp_plan(
        IspConfig(topology="polska"),
        topology=NetworkTopology(
            name=name,
            source_format="sndlib-xml",
            meta={},
            nodes=(
                TopoNode(id="C"),
                TopoNode(id="A"),
                TopoNode(id="B"),
                TopoNode(id="D"),
            ),
            links=(
                TopoLink(id="L1", source="A", target="B"),
                TopoLink(id="L2", source="B", target="C"),
                TopoLink(id="L3", source="C", target="D"),
            ),
            demands=(),
        ),
    )


def test_none_mode_returns_none() -> None:
    assert compile_bgp_plan(_isp_plan(), "none") is None


def test_ibgp_rr_stable_and_roles() -> None:
    u = _isp_plan()
    plan = compile_bgp_plan(u, "ibgp_rr")
    assert plan is not None
    devices = sorted(n.device_name for n in u.nodes)
    assert plan.mode == "ibgp_rr"
    rrs = [n for n in plan.nodes if "rr" in n.roles]
    clients = [n for n in plan.nodes if "client" in n.roles]
    assert {n.device_name for n in rrs} == set(devices[:2])
    assert {n.device_name for n in clients} == set(devices[2:])
    assert all(n.asn == 65000 for n in plan.nodes)
    # Sessions use loopbacks
    assert all(s.update_source == "lo" for s in plan.sessions)
    assert all(s.session_type == "ibgp" for s in plan.sessions)
    # RR marks clients
    assert any(s.route_reflector_client for s in plan.sessions)
    plan2 = compile_bgp_plan(u, "ibgp_rr")
    assert plan.inventory == plan2.inventory


def test_ebgp_partition_and_session_structure() -> None:
    u = _isp_plan()
    plan = compile_bgp_plan(u, "ebgp")
    assert plan is not None
    asns = {n.asn for n in plan.nodes}
    assert asns == {65001, 65002, 65003}
    asn_of = {n.device_name: n.asn for n in plan.nodes}
    for sess in plan.sessions:
        if sess.session_type == "ebgp":
            assert asn_of[sess.local_device] != asn_of[sess.remote_device]
            assert sess.update_source is None
        else:
            assert asn_of[sess.local_device] == asn_of[sess.remote_device]
            assert sess.update_source == "lo"
    assert plan.originated
    # Observers are direct eBGP peers (no iBGP to flood within an AS).
    peer_of = {(s.local_device, s.remote_device) for s in plan.sessions}
    for observer, prefix in plan.expect_reachable:
        origin = next(o.device for o in plan.originated if o.prefix == prefix)
        assert (origin, observer) in peer_of
    # FRR has route-maps and eBGP policy hooks
    node = plan.nodes[0]
    frag = render_bgp_frr_fragment(node, plan)
    assert "route-map BGP-OUT" in frag
    assert "route-map BGP-IN" in frag
    assert "router bgp" in frag


def test_frr_merge_only_when_bgp() -> None:
    u = _isp_plan()
    igp = u.nodes[0].frr_conf
    assert "router bgp" not in igp
    plan = compile_bgp_plan(u, "ibgp_rr")
    assert plan is not None
    bgp_node = next(n for n in plan.nodes if n.device_name == u.nodes[0].device_name)
    merged = merge_frr_conf(igp, render_bgp_frr_fragment(bgp_node, plan))
    assert "router isis" in merged or "router ospf" in merged
    assert "router bgp" in merged
    assert "route-reflector-client" in merged or "neighbor" in merged


def test_abilene_ebgp_rpki_profile() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene"))
    plan = compile_bgp_plan(isp_plan, "ebgp", rpki=True)
    assert plan is not None
    assert plan.mode == "ebgp"
    inv = plan.inventory
    assert inv.get("rpki") is True
    leaker = inv.get("leaker_device")
    rov = inv.get("rov_observer")
    non_rov = inv.get("non_rov_observer")
    assert leaker and rov and non_rov
    assert leaker != rov and leaker != non_rov
    assert inv.get("legitimate_origin_asn") == 65001
    assert inv.get("leaker_asn") == 65002
    assert "203.0.113.0/24" in inv.get("leak_prefixes")
    assert any(s.session_type == "ibgp" for s in plan.sessions)
    assert any(s.session_type == "ebgp" for s in plan.sessions)
    leaker_node = next(n for n in plan.nodes if n.device_name == leaker)
    assert "leaker" in leaker_node.roles
    assert leaker_node.export_deny_prefixes
    rov_node = next(n for n in plan.nodes if n.device_name == rov)
    assert rov_node.rov_reject_invalid
    assert rov_node.rpki_cache is not None
    frag = render_bgp_frr_fragment(rov_node, plan)
    assert "rpki cache tcp" in frag
    assert "match rpki invalid" in frag
    leak_frag = render_bgp_frr_fragment(leaker_node, plan)
    assert "prefix-list LEAK" in leak_frag
    assert "route-map BGP-OUT deny 5" in leak_frag


def test_geant_ebgp_rpki_profile() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="geant", igp="ospf"))
    plan = compile_bgp_plan(isp_plan, "ebgp", rpki=True)
    assert plan is not None
    inv = plan.inventory
    assert inv.get("rpki") is True
    assert inv.get("leaker_device")
    assert inv.get("rov_observer")
    assert inv.get("non_rov_observer")
    assert inv.get("leaker_device") != inv.get("rov_observer")
    asn_of = {n.device_name: n.asn for n in plan.nodes}
    assert asn_of[inv["leaker_device"]] == 65002
    assert asn_of[inv["rov_observer"]] == 65003
    assert asn_of[inv["non_rov_observer"]] == 65001


def test_abilene_ebgp_rtbh_profile() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    plan = compile_bgp_plan(isp_plan, "ebgp", rtbh=True)
    assert plan is not None
    inv = plan.inventory
    assert inv.get("rtbh") is True
    leaker = inv.get("leaker_device")
    provider = inv.get("rtbh_provider_device")
    origin = inv.get("legitimate_origin_device")
    assert leaker and provider and origin
    assert inv.get("target_prefix") == "198.51.100.0/24"
    assert inv.get("blackhole_community") == "65003:666"
    assert inv.get("leaker_to_rtbh_neighbor_ip")
    provider_node = next(n for n in plan.nodes if n.device_name == provider)
    assert provider_node.rtbh_import_policy
    frag = render_bgp_frr_fragment(provider_node, plan)
    assert "community-list standard BH-SIGNAL permit 65003:666" in frag
    assert "match community BH-SIGNAL" in frag
    assert "set ip next-hop 192.0.2.1" in frag
    leaker_node = next(n for n in plan.nodes if n.device_name == leaker)
    assert leaker_node.ebgp_outbound_route_maps
    leak_frag = render_bgp_frr_fragment(leaker_node, plan)
    assert "route-map BGP-OUT-TO-65003 permit 10" in leak_frag
    assert "set local-preference 200" in frag


def test_dfn_bwin_ebgp_rtbh_profile() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="dfn-bwin", igp="ospf"))
    plan = compile_bgp_plan(isp_plan, "ebgp", rtbh=True)
    assert plan is not None
    inv = plan.inventory
    assert inv.get("rtbh") is True
    assert inv.get("leaker_device")
    assert inv.get("rtbh_provider_device")
    assert inv.get("leaker_to_rtbh_neighbor_ip")


def test_rpki_flag_enables_profile() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene"))
    with_rpki = compile_bgp_plan(isp_plan, "ebgp", rpki=True)
    without = compile_bgp_plan(isp_plan, "ebgp", rpki=False)
    assert with_rpki is not None and without is not None
    assert with_rpki.inventory.get("rpki") is True
    assert not without.inventory.get("rpki")
    assert with_rpki.inventory["leaker_device"]
    assert with_rpki.inventory["rov_observer"]


def test_rpki_requires_ebgp_mode() -> None:
    with pytest.raises(BgpConfigError, match="requires bgp_mode 'ebgp'"):
        compile_bgp_plan(_isp_plan(), "ibgp_rr", rpki=True)


def test_non_abilene_ebgp_unchanged() -> None:
    u = _isp_plan()
    plan = compile_bgp_plan(u, "ebgp")
    assert plan is not None
    assert not plan.inventory.get("rpki")
    assert any(s.session_type == "ebgp" for s in plan.sessions)
    assert any(s.session_type == "ibgp" for s in plan.sessions)


def test_ebgp_as_regions_are_connected_and_boundaries_are_igp_passive() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp")
    assert bgp is not None
    assert compile_bgp_plan(isp_plan, "ebgp").inventory == bgp.inventory
    asn_of = {node.device_name: node.asn for node in bgp.nodes}
    graph = {node.device_name: set() for node in isp_plan.nodes}
    for link in isp_plan.links:
        graph[link.endpoint_a].add(link.endpoint_b)
        graph[link.endpoint_b].add(link.endpoint_a)
    for asn in sorted(set(asn_of.values())):
        members = {device for device, value in asn_of.items() if value == asn}
        reached = {min(members)}
        queue = list(reached)
        for device in queue:
            for peer in graph[device] & members:
                if peer not in reached:
                    reached.add(peer)
                    queue.append(peer)
        assert reached == members

    scoped = scope_igp_to_bgp_as(isp_plan, bgp)
    for node in scoped.nodes:
        for interface in node.interfaces:
            crosses_as = asn_of[node.device_name] != asn_of[interface.peer_device]
            assert interface.passive is crosses_as
    attachment = attach_traffic_stubs(
        scoped,
        TrafficMatrixSeries(
            topology="abilene",
            source="test",
            intervals=(TrafficInterval(index=0, duration_sec=1, flows=()),),
            sample_period_sec=1,
            unit_note="test",
            path=None,
        ),
        pop_node_ids=tuple(node.node_id for node in scoped.nodes),
    )
    assert attachment.plan.inventory["igp_scope"] == "per_as"
    assert attachment.plan.inventory["igp_passive_boundary_links"]


@pytest.mark.parametrize("topology", list_sndlib_topologies())
def test_catalog_ebgp_has_connected_as_scoped_igp_and_linear_sessions(
    topology: str,
) -> None:
    isp_plan = compile_isp_plan(IspConfig(topology=topology, igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp")
    assert bgp is not None
    rebuilt = compile_bgp_plan(isp_plan, "ebgp")
    assert rebuilt is not None and rebuilt.inventory == bgp.inventory
    scoped = scope_igp_to_bgp_as(isp_plan, bgp)
    asn_of = {node.device_name: node.asn for node in bgp.nodes}
    as_regions = {
        frozenset(device for device, value in asn_of.items() if value == asn)
        for asn in set(asn_of.values())
    }
    assert {frozenset(component) for component in igp_components(scoped)} == as_regions
    assert all(
        asn_of[link.endpoint_a] == asn_of[link.endpoint_b]
        for link in active_igp_links(scoped)
    )
    ibgp_sessions = [
        session for session in bgp.sessions if session.session_type == "ibgp"
    ]
    assert len(ibgp_sessions) == 2 * (len(bgp.nodes) - len(as_regions))
    traffic = attach_traffic_stubs(
        scoped,
        TrafficMatrixSeries(
            topology=topology,
            source="test",
            intervals=(TrafficInterval(index=0, duration_sec=1, flows=()),),
            sample_period_sec=1,
            unit_note="test",
            path=None,
        ),
        pop_node_ids=tuple(node.node_id for node in scoped.nodes),
    )
    contract = build_isp_validation_contract(
        traffic.plan, traffic=traffic, bgp_plan=bgp
    )
    ospf_intents = [
        intent
        for intent in contract.intents
        if intent.adjacency is not None and intent.adjacency.protocol == "ospf"
    ]
    assert len(ospf_intents) == len(active_igp_links(traffic.plan))
    assert (
        contract.to_json()
        == build_isp_validation_contract(
            traffic.plan, traffic=traffic, bgp_plan=bgp
        ).to_json()
    )
