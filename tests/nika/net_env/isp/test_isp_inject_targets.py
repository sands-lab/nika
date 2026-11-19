"""Unit tests for isp inject target helpers (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.isp.inject_targets import (
    DEFAULT_HIJACK_PREFIX,
    first_link_endpoint,
    first_originator,
    first_router,
    hijack_speaker_and_prefix,
    isp_inject_params,
)
from nika.net_env.isp.bgp import compile_bgp_plan
from nika.net_env.isp.igp import IspConfig, compile_isp_plan

pytestmark = pytest.mark.unit


def test_containerlab_ospf_cut_edges_keep_active_igp_links() -> None:
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.problems.routing_control_plane.ospf import ospf_host_cut_edges

    env = get_net_env_instance(
        "isp_abilene", backend="containerlab", igp="ospf", bgp_mode="ebgp"
    )
    targets = ospf_host_cut_edges(env)
    assert ("atlang", "e1-1") in targets
    assert ("atlam5", "e1-1") in targets
    # Cross-AS links are passive IGP boundaries even when they connect routers.
    assert ("atlang", "e1-3") not in targets


def test_link_and_router_targets_polska() -> None:
    plan = compile_isp_plan(IspConfig(topology="polska"))
    inv = plan.inventory
    device, iface = first_link_endpoint(inv)
    assert device
    assert iface.startswith("eth")
    assert first_router(inv) == sorted(n["device"] for n in inv["nodes"])[0]
    params = isp_inject_params("link_down", inv)
    assert params == {"host_name": device, "intf_name": iface}
    assert isp_inject_params("link_capacity_bottleneck", inv) == {
        "host_name": device,
        "intf_name": iface,
    }
    assert isp_inject_params("link_packet_corruption", inv) == {
        "host_name": device,
        "intf_name": iface,
    }
    assert isp_inject_params("frr_service_down", inv) == isp_inject_params(
        "routing_service_down", inv
    )
    assert isp_inject_params("routing_service_down", inv) == {
        "host_name": first_router(inv)
    }


def test_isp_link_symptom_targets_abilene() -> None:
    from nika.net_env.isp.inject_targets import isp_link_symptom_targets
    from nika.net_env.isp.kathara.lab import _stub_series_all_routers
    from nika.net_env.isp.traffic.stubs import attach_traffic_stubs

    plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    attachment = attach_traffic_stubs(
        plan,
        _stub_series_all_routers(plan),
        pop_node_ids=tuple(n.node_id for n in plan.nodes),
    )
    inv = attachment.plan.inventory
    device, iface = first_link_endpoint(inv)
    targets = isp_link_symptom_targets(inv, device, iface)
    assert targets["symptom_host"].startswith("pc_")
    assert targets["peer_host"].startswith("pc_")
    assert targets["probe_dst_ip"].startswith("10.254.")


def test_bgp_originator_and_hijack() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="polska"))
    for mode in ("ibgp_rr", "ebgp"):
        bgp = compile_bgp_plan(isp_plan, mode)
        assert bgp is not None
        bgp_inv = bgp.inventory
        origin = first_originator(bgp_inv)
        assert origin in {o["device"] for o in bgp_inv["originated"]}
        assert isp_inject_params("bgp_asn_misconfig", isp_plan.inventory, bgp_inv) == {
            "host_name": origin
        }
        missing = isp_inject_params(
            "bgp_missing_route_advertisement", isp_plan.inventory, bgp_inv
        )
        assert missing["host_name"] == origin
        assert missing.get("prefix")
        assert any(
            o["device"] == origin and o["prefix"] == missing["prefix"]
            for o in bgp_inv["originated"]
        )
        hijack = isp_inject_params("bgp_hijacking", isp_plan.inventory, bgp_inv)
        speaker, prefix = hijack_speaker_and_prefix(bgp_inv)
        assert hijack == {"host_name": speaker, "target_network": prefix}
        assert prefix == DEFAULT_HIJACK_PREFIX
        originators = {o["device"] for o in bgp_inv["originated"]}
        if len(bgp_inv["nodes"]) > len(originators):
            assert speaker not in originators


def test_bgp_rtbh_leak_target_from_inventory() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp", rtbh=True)
    assert bgp is not None
    params = isp_inject_params(
        "bgp_blackhole_community_leak", isp_plan.inventory, bgp.inventory
    )
    assert params == {"host_name": bgp.inventory["leaker_device"]}
    from nika.net_env.isp.inject_targets import isp_bgp_symptom_targets

    symptoms = isp_bgp_symptom_targets(
        isp_plan.inventory,
        bgp.inventory,
        bgp.inventory["leaker_device"],
        "bgp_blackhole_community_leak",
    )
    assert symptoms["symptom_host"] == bgp.inventory["data_plane_observer_host"]
    assert symptoms["probe_dst_ip"] == "198.51.100.1"


def test_default_probe_path_stays_inside_one_as_under_ebgp() -> None:
    from nika.net_env.isp.inject_targets import isp_topology_probe_path

    plan = compile_isp_plan(IspConfig(topology="dfn-bwin"))
    asn_of = {n.device_name: n.asn for n in compile_bgp_plan(plan, "ebgp").nodes}
    path = isp_topology_probe_path("dfn-bwin", "ebgp")
    peer_router = path.peer_host.removeprefix("pc_")
    assert asn_of[path.control_plane_host] == asn_of[peer_router]
    # Other modes keep the first backbone link, which crosses ASes here.
    assert isp_topology_probe_path("dfn-bwin") != path
    from nika.net_env.net_env_pool import get_probe_path
    from nika.problems.rca.inventory import load_offline_net_env

    env = load_offline_net_env("isp_dfn-bwin", bgp_mode="ebgp")
    assert get_probe_path("isp_dfn-bwin", net_env=env) == path


@pytest.mark.parametrize("topology", ["nobel-us", "telecomserbia", "arpanet196912"])
@pytest.mark.parametrize(
    "problem", ["bgp_missing_route_advertisement", "bgp_hijacking"]
)
def test_security_profiles_keep_default_bgp_targets(topology, problem) -> None:
    """The RPKI profile's content prefixes do not move other faults' targets."""
    from nika.net_env.isp.bgp import compile_bgp_plan, default_ebgp_profiles
    from nika.net_env.isp.igp import IspConfig, compile_isp_plan
    from nika.net_env.isp.inject_targets import isp_inject_params

    isp_plan = compile_isp_plan(IspConfig(topology=topology, igp="ospf"))
    plain = compile_bgp_plan(isp_plan, "ebgp").inventory
    profiled = compile_bgp_plan(
        isp_plan, "ebgp", **default_ebgp_profiles(isp_plan)
    ).inventory
    assert isp_inject_params(problem, isp_plan.inventory, profiled) == (
        isp_inject_params(problem, isp_plan.inventory, plain)
    )


@pytest.mark.parametrize("scenario", ["isp_abilene", "isp_geant"])
def test_sonic_customer_target_matches_containerlab_attachment(scenario):
    from nika.net_env.net_env_pool import get_net_env_instance
    from nika.net_env.isp.inject_targets import customer_route_session
    from nika.net_env.utils.sonic.common import sonic_port
    from nika.problems.routing_control_plane.bgp import _compile_isp_bgp_inventory

    env = get_net_env_instance(
        scenario,
        igp="ospf",
        bgp_mode="ebgp",
        backend="containerlab",
        device_profile="sonic",
    )
    _plan, inventory = _compile_isp_bgp_inventory(
        scenario,
        igp="ospf",
        bgp_mode="ebgp",
        backend="containerlab",
        device_profile="sonic",
    )
    assert customer_route_session(inventory) == customer_route_session(
        env.bgp_plan.inventory
    )
    site = env.bgp_plan.customer_site
    document = env._build_clab_document(lab_name="customer-contract")
    assert site.host_name in document["topology"]["nodes"]
    assert site.host_name in env.hosts
    provider = next(
        node for node in env.plan.nodes if node.device_name == site.provider
    )
    interface = env.router_iface(env.bgp_plan.customer_interface(provider))
    assert {
        "endpoints": [f"{site.provider}:{interface}", f"{site.host_name}:eth1"]
    } in document["topology"]["links"]
    assert (
        f"{site.gateway}/24"
        in env.sonic_routers[site.provider].addresses[sonic_port(interface)]
    )
    assert any(host.address == site.address for host in env._linux_hosts)


@pytest.mark.parametrize("scenario", ["isp_abilene", "isp_dfn-bwin", "isp_geant"])
def test_rpki_and_customer_attachments_have_separate_interfaces(scenario):
    from nika.net_env.net_env_pool import get_net_env_instance

    env = get_net_env_instance(scenario, igp="ospf", bgp_mode="ebgp")
    rtr = env._rpki_attachment
    machine = env.lab.machines[rtr["router"]]
    interface = machine.interfaces[int(rtr["router_iface"][3:])]
    assert interface.link.name == env.bgp_plan.inventory["rpki_rtr"]["collision_domain"]
    site = env.bgp_plan.customer_site
    if site.provider == rtr["router"]:
        provider = next(
            node for node in env.plan.nodes if node.device_name == site.provider
        )
        assert rtr["router_iface"] != env.bgp_plan.customer_interface(provider)
