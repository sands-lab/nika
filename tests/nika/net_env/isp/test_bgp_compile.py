"""Unit tests for ISP BGP presets (no Docker)."""

from __future__ import annotations

from nika.net_env.isp.bgp import (
    compile_bgp_plan,
    merge_frr_conf,
    render_bgp_frr_fragment,
)
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
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


def test_ebgp_partition_and_cross_only() -> None:
    u = _isp_plan()
    plan = compile_bgp_plan(u, "ebgp")
    assert plan is not None
    asns = {n.asn for n in plan.nodes}
    assert asns == {65001, 65002, 65003}
    # No intra-AS sessions
    asn_of = {n.device_name: n.asn for n in plan.nodes}
    for sess in plan.sessions:
        assert sess.session_type == "ebgp"
        assert asn_of[sess.local_device] != asn_of[sess.remote_device]
        assert sess.update_source is None
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
    plan = compile_bgp_plan(isp_plan, "ebgp")
    assert plan is not None
    inv = plan.inventory
    assert inv.get("rpki") is True
    assert inv.get("leaker_device") == "losang"
    assert inv.get("rov_observer") == "snvang"
    assert inv.get("non_rov_observer") == "atlang"
    assert inv.get("legitimate_origin_asn") == 65001
    assert inv.get("leaker_asn") == 65002
    assert "203.0.113.0/24" in inv.get("leak_prefixes")
    # Intra-AS iBGP present alongside cross-AS eBGP.
    assert any(s.session_type == "ibgp" for s in plan.sessions)
    assert any(s.session_type == "ebgp" for s in plan.sessions)
    losang = next(n for n in plan.nodes if n.device_name == "losang")
    assert "leaker" in losang.roles
    assert losang.export_deny_prefixes
    snvang = next(n for n in plan.nodes if n.device_name == "snvang")
    assert snvang.rov_reject_invalid
    assert snvang.rpki_cache is not None
    frag = render_bgp_frr_fragment(snvang, plan)
    assert "rpki cache tcp" in frag
    assert "match rpki invalid" in frag
    leak_frag = render_bgp_frr_fragment(losang, plan)
    assert "prefix-list LEAK" in leak_frag
    assert "route-map BGP-OUT deny 5" in leak_frag


def test_non_abilene_ebgp_unchanged() -> None:
    u = _isp_plan()
    plan = compile_bgp_plan(u, "ebgp")
    assert plan is not None
    assert not plan.inventory.get("rpki")
    assert all(s.session_type == "ebgp" for s in plan.sessions)


def test_order_independence() -> None:
    topo_a = NetworkTopology(
        name="tiny",
        source_format="sndlib-xml",
        meta={},
        nodes=(TopoNode(id="A"), TopoNode(id="B"), TopoNode(id="C")),
        links=(
            TopoLink(id="L1", source="A", target="B"),
            TopoLink(id="L2", source="B", target="C"),
        ),
        demands=(),
    )
    topo_b = NetworkTopology(
        name="tiny",
        source_format="sndlib-xml",
        meta={},
        nodes=tuple(reversed(topo_a.nodes)),
        links=tuple(reversed(topo_a.links)),
        demands=(),
    )
    ua = compile_isp_plan(IspConfig(topology="polska"), topology=topo_a)
    ub = compile_isp_plan(IspConfig(topology="polska"), topology=topo_b)
    pa = compile_bgp_plan(ua, "ibgp_rr")
    pb = compile_bgp_plan(ub, "ibgp_rr")
    assert pa is not None and pb is not None
    assert pa.inventory == pb.inventory
