"""RouterOS profiles keep each environment's links, addressing and protocol intents."""

import pytest

from nika.net_env.utils.routeros.common import CONFIG_FILE_PATH


def _build(scenario: str, profile: str):
    from nika.net_env.net_env_pool import get_net_env_instance

    return get_net_env_instance(scenario, topo_size="s", device_profile=profile)


def _assert_links_unchanged(native, frr):
    """Converted routers keep FRR's container interfaces; vrnetlab sees dp*."""
    assert native.native_routers
    for name in native.native_routers:
        machine = native.lab.machines[name]
        links = {k: v.link.name for k, v in machine.interfaces.items()}
        assert links == {
            k: v.link.name for k, v in frr.lab.machines[name].interfaces.items()
        }
        envs = machine.meta["envs"]
        assert envs["CLAB_INTF_PREFIX"] == "dp"
        assert envs["CLAB_INTFS"] == str(max(links) + 1)
        assert "routeros" in native.machine_identities[name].capabilities
        assert f"/system identity set name={name}" in machine.fs.readtext(
            CONFIG_FILE_PATH
        )


@pytest.mark.unit
def test_dc_clos_routeros_renders_every_ebgp_session():
    native, frr = _build("dc_clos", "routeros"), _build("dc_clos", "frr")
    _assert_links_unchanged(native, frr)
    assert set(native.native_routers) == set(native.bgp_neighbors)
    for name, peers in native.bgp_neighbors.items():
        config = native.deployment_configs[name]
        for peer in peers:
            assert f"remote.address={peer} " in config
    # Container eth2 of the leaf is RouterOS ether4.
    leaf = native.deployment_configs["leaf_router_0_0"]
    assert "address=10.0.0.1/24 interface=ether4" in leaf
    assert "list=BGP-NETWORKS address=10.0.0.0/24" in leaf
    with pytest.raises(ValueError, match="supports size s only"):
        from nika.net_env.net_env_pool import get_net_env_instance

        get_net_env_instance("dc_clos", topo_size="m", device_profile="routeros")


@pytest.mark.unit
def test_campus_routeros_routes_bridged_gateways_and_relays_dhcp():
    native, frr = _build("campus_lan", "routeros"), _build("campus_lan", "frr")
    _assert_links_unchanged(native, frr)
    core = native.deployment_configs["router_core_1"]
    assert "area-id=0.0.0.0" in core and "area-id=0.0.0.1" in core
    dist = native.deployment_configs["router_dist_1_1"]
    assert "/interface bridge port add bridge=br0 interface=ether3" in dist
    assert "address=10.1.1.1/24 interface=br0" in dist
    assert "dhcp-server=10.200.0.8 local-address=10.1.1.1" in dist


@pytest.mark.unit
def test_enterprise_routeros_keeps_vrf_leaking_tunnels_and_guest_nat():
    native, frr = (
        _build("enterprise_branch", "routeros"),
        _build("enterprise_branch", "frr"),
    )
    _assert_links_unchanged(native, frr)
    assert set(native.native_routers) == {"hq_edge", "dc2_edge", "br1_edge", "br2_edge"}
    branch = native.deployment_configs["br1_edge"]
    # Business VRFs import only overlay BGP routes; guest imports nothing.
    assert "/routing bgp vpn add name=vrf_corp vrf=vrf_corp" in branch
    assert "import.route-targets=65001:1 export.route-targets=65001:10" in branch
    assert "vrf=vrf_guest" not in branch
    assert "to-addresses=198.18.1.10" in branch
    # The primary WAN keeps container eth2, RouterOS ether4.
    assert native.primary_wan_interface() == frr.primary_wan_interface() == "eth2"
    assert "address=198.18.1.10/32 interface=ether4" in branch
    assert 'chain=POLICY-OUT rule="if (dst == 10.1.10.0/24) { accept; }"' in branch
    hub = native.deployment_configs["hq_edge"]
    assert "nexthop-choice=force-self" in hub
    assert "queue tree add name=wg_br1-ef parent=wg_br1" in hub
    # WAN propagation delay stays on the container link.
    assert "tc qdisc replace dev eth2 root netem" in native.lab.fs.readtext(
        "br1_edge.startup"
    )


@pytest.mark.unit
def test_translation_rejects_policies_routeros_cannot_keep():
    from nika.net_env.utils.routeros.frr import translate_frr_router

    startup = "ip addr add 10.0.0.1/31 dev eth0\nservice frr start\n"
    with pytest.raises(ValueError, match="open eBGP policy"):
        translate_frr_router(
            "r1", "router bgp 1\n neighbor 10.0.0.0 remote-as 2\n", startup
        )
    with pytest.raises(ValueError, match="Unsupported FRR statement"):
        translate_frr_router("r1", "router ospf\n redistribute connected\n", startup)


@pytest.mark.unit
def test_failure_providers_follow_the_router_profile():
    from types import SimpleNamespace

    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps
    from nika.problems.support.router_ops import native_fault_ops, router_fault_ops
    from nika.runtime.base import RuntimeCapabilityError

    def problem(profile):
        return SimpleNamespace(net_env=_build("dc_clos", profile), runtime=None)

    leaf = "leaf_router_0_0"
    ops = router_fault_ops(problem("routeros"), leaf)
    assert isinstance(ops, RouterOsFaultOps)
    assert isinstance(
        native_fault_ops(problem("routeros"), leaf, "block_bgp_transport"),
        RouterOsFaultOps,
    )
    # FRR routers keep the default path; native routers never fall back to it.
    assert router_fault_ops(problem("frr"), leaf) is None
    assert native_fault_ops(problem("frr"), leaf, "block_bgp_transport") is None
    with pytest.raises(RuntimeCapabilityError, match="not supported on iosxr"):
        native_fault_ops(problem("iosxr"), leaf, "set_tunnel_peer_public_key")
    # RouterOS runs no separate routing daemon to stop.
    with pytest.raises(RuntimeCapabilityError, match="not supported on routeros"):
        native_fault_ops(problem("routeros"), leaf, "stop_routing_process")
