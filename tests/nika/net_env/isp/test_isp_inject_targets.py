"""Unit tests for isp inject target helpers (no Docker)."""

from __future__ import annotations

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
    assert isp_inject_params("frr_service_down", inv) == {
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
        hijack = isp_inject_params("bgp_hijacking", isp_plan.inventory, bgp_inv)
        speaker, prefix = hijack_speaker_and_prefix(bgp_inv)
        assert hijack == {"host_name": speaker, "target_network": prefix}
        assert prefix == DEFAULT_HIJACK_PREFIX
        originators = {o["device"] for o in bgp_inv["originated"]}
        if len(bgp_inv["nodes"]) > len(originators):
            assert speaker not in originators


def test_bgp_rpki_leak_target_from_inventory() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene", igp="ospf"))
    bgp = compile_bgp_plan(isp_plan, "ebgp", rpki=True)
    assert bgp is not None
    params = isp_inject_params(
        "bgp_rpki_invalid_route_leak", isp_plan.inventory, bgp.inventory
    )
    assert params == {"host_name": bgp.inventory["leaker_device"]}


def test_bgp_max_prefix_target_ebgp() -> None:
    isp_plan = compile_isp_plan(IspConfig(topology="abilene"))
    bgp = compile_bgp_plan(isp_plan, "ebgp")
    assert bgp is not None
    params = isp_inject_params(
        "bgp_max_prefix_exceeded", isp_plan.inventory, bgp.inventory
    )
    assert params["receiver_name"]
    assert params["peer_name"]
    assert params["neighbor_ip"]
    assert params["receiver_name"] != params["peer_name"]
    pair = {params["receiver_name"], params["peer_name"]}
    sessions = [
        s
        for s in bgp.inventory["sessions"]
        if s["session_type"] == "ebgp"
        and {s["local_device"], s["remote_device"]} == pair
    ]
    assert sessions
