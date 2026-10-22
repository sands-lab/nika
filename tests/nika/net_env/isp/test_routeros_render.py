"""RouterOS rendering preserves typed ISP routing and policy contracts."""

from dataclasses import replace

import pytest

from nika.net_env.isp.bgp import compile_bgp_plan, scope_igp_to_bgp_as
from nika.net_env.isp.bgp.policy import BgpPolicyTerm
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.igp.frr import isis_net_from_router_id
from nika.net_env.isp.routeros import render_routeros_isp, routeros_interface
from nika.topology.models import NetworkTopology, TopoLink, TopoNode

pytestmark = pytest.mark.unit


def _plan():
    return compile_isp_plan(
        IspConfig(igp="ospf", first_interface_index=1),
        topology=NetworkTopology(
            name="chain",
            source_format="sndlib-xml",
            meta={},
            nodes=tuple(TopoNode(id=name) for name in "ABCD"),
            links=tuple(
                TopoLink(id=f"L{index}", source=left, target=right)
                for index, (left, right) in enumerate(zip("ABC", "BCD"))
            ),
            demands=(),
        ),
    )


def test_ospf_uses_typed_costs_and_only_active_neighbors():
    plan = _plan()
    node = plan.nodes[0]
    iface = replace(node.interfaces[0], name="eth1", metric=37, passive=True)
    plan = replace(plan, nodes=(replace(node, interfaces=(iface,)), *plan.nodes[1:]))
    config = render_routeros_isp(plan)[node.device_name]
    assert routeros_interface("lo") == "lo0"
    assert routeros_interface("eth0") == "ether1"
    assert routeros_interface("eth1") == "ether2"
    assert f"address={iface.address}/{iface.prefixlen} interface=ether2" in config
    assert "type=broadcast cost=37 passive" in config
    assert "/routing ospf static-neighbor" not in config
    active_config = render_routeros_isp(plan)[plan.nodes[1].device_name]
    assert "type=ptp cost=10" in active_config
    assert "/routing ospf static-neighbor" not in active_config


@pytest.mark.parametrize("mode", ["ibgp_rr", "ebgp"])
def test_bgp_keeps_origins_outside_ospf_and_preserves_sessions(mode):
    plan = _plan()
    bgp = compile_bgp_plan(plan, mode)
    assert bgp is not None
    plan = scope_igp_to_bgp_as(plan, bgp)
    rendered = render_routeros_isp(plan, bgp)
    for node in bgp.nodes:
        config = rendered[node.device_name]
        assert f"name=CORE as={node.asn} router-id={node.router_id}" in config
        for origin in node.originated:
            assert f"address={origin.ping_address}/24 interface=lo1" in config
            assert f"list=BGP-ORIGINS address={origin.prefix}" in config
        assert not any(
            "lo1" in line or any(origin.prefix in line for origin in bgp.originated)
            for line in config.splitlines()
            if line.startswith("/routing ospf")
        )
        for session in node.sessions:
            role = "ibgp-rr" if session.route_reflector_client else session.session_type
            assert f"local.role={role} local.address={session.local_ip}" in config
            assert (
                f"remote.address={session.remote_ip} remote.as={session.remote_asn}"
                in config
            )
        assert ("nexthop-choice=default" in config) == (mode == "ebgp")
        assert "nexthop-choice=force-self" not in config


def test_import_policy_and_unsupported_profiles():
    plan = _plan()
    bgp = compile_bgp_plan(plan, "ebgp")
    assert bgp is not None
    node = bgp.nodes[0]
    peer = node.sessions[0].remote_ip
    node = replace(
        node,
        import_policies=(
            (
                peer,
                (
                    BgpPolicyTerm(
                        prefix="198.51.100.0/24", permit=True, local_preference=250
                    ),
                ),
            ),
        ),
    )
    bgp = replace(bgp, nodes=(node, *bgp.nodes[1:]))
    config = render_routeros_isp(plan, bgp)[node.device_name]
    assert "input.filter=NC-IN-0" in config
    assert (
        "if (dst == 198.51.100.0/24) { set bgp-local-pref 250; jump BGP-IN; }" in config
    )
    with pytest.raises(ValueError, match="security profiles"):
        render_routeros_isp(plan, replace(bgp, inventory={"rtbh": True}))
    node = plan.nodes[0]
    wide = replace(node, interfaces=node.interfaces * 31)
    with pytest.raises(ValueError, match="at most 30 data interfaces"):
        render_routeros_isp(replace(plan, nodes=(wide, *plan.nodes[1:])))


def test_isis_matches_frr_level2_domain_and_passive_edges():
    plan = _plan()
    node = plan.nodes[0]
    edge = replace(node.interfaces[0], name="eth2", metric=37, passive=True)
    plan = replace(
        replace(plan, igp="isis"),
        nodes=(replace(node, interfaces=(*node.interfaces, edge)), *plan.nodes[1:]),
    )
    config = render_routeros_isp(plan)[node.device_name]
    system_id = ".".join(isis_net_from_router_id(node.router_id).split(".")[2:5])
    assert f"areas=49.0001 system-id={system_id} metric-type=wide" in config
    assert "interfaces=lo0 levels=l2 passive" in config
    active = node.interfaces[0]
    assert (
        f"interfaces={routeros_interface(active.name)} levels=l2 ptp "
        f"ptp.l2.metric={active.metric}\n" in config
    )
    assert "interfaces=ether3 levels=l2 passive\n" in config
    assert "/routing ospf" not in config
