"""Unit tests for multi-backend isp pool resolution and SRL render (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.isp.bgp import compile_bgp_plan, scope_igp_to_bgp_as
from nika.net_env.isp.bgp.srl import render_bgp_srl_block
from nika.net_env.isp.igp import IspConfig, compile_isp_plan
from nika.net_env.isp.igp.ifaces import srl_e1_name, srl_ethernet_name
from nika.net_env.isp.igp.srl import render_srl_node_config
from nika.net_env.isp.traffic import (
    attach_traffic_stubs,
    remap_inventory_ifaces_to_srl,
    resolve_traffic_series,
)
from nika.net_env.net_env_pool import (
    get_net_env_instance,
    resolve_scenario_backend,
    scenario_supported_backends,
)


def test_isp_supports_both_backends() -> None:
    assert scenario_supported_backends("isp") == ["kathara", "containerlab"]


def test_resolve_isp_defaults_to_kathara() -> None:
    assert (
        resolve_scenario_backend("isp", default_when_ambiguous="kathara") == "kathara"
    )


def test_resolve_isp_requires_choice_without_default() -> None:
    with pytest.raises(ValueError, match="pass --backend"):
        resolve_scenario_backend("isp")


def test_clab_rejects_kathara_only_rpki_mode() -> None:
    with pytest.raises(ValueError, match="Kathara"):
        get_net_env_instance(
            "isp",
            backend="containerlab",
            topo="abilene",
            bgp_mode="ebgp",
            rpki=True,
            device_profile="nokia_srlinux",
        )


def test_iface_mapping() -> None:
    assert srl_e1_name("eth0") == "e1-1"
    assert srl_ethernet_name("eth0") == "ethernet-1/1"
    assert srl_e1_name("eth2") == "e1-3"


def test_srl_render_isis_pdh() -> None:
    plan = compile_isp_plan(IspConfig(topology="pdh", igp="isis"))
    node = plan.nodes[0]
    text = render_srl_node_config(node, igp="isis")
    assert "srl_nokia-isis:isis" in text
    assert "ethernet-1/1" in text
    assert f"{node.loopback}/32" in text


def test_srl_render_ospf_and_passive_edge() -> None:
    plan = compile_isp_plan(IspConfig(topology="pdh", igp="ospf"))
    series = resolve_traffic_series("pdh", "demands")
    assert series is not None
    attachment = attach_traffic_stubs(plan, series, host_iface="eth1", render_frr=False)
    remapped = remap_inventory_ifaces_to_srl(attachment)
    node = remapped.plan.nodes[0]
    assert any(i.passive for i in node.interfaces)
    text = render_srl_node_config(node, igp="ospf")
    assert "srl_nokia-ospf:ospf" in text
    assert "passive" in text
    edge = remapped.edge_links[0]
    assert edge.router_iface.startswith("e1-")


def test_srl_bgp_block() -> None:
    plan = compile_isp_plan(IspConfig(topology="pdh", igp="isis"))
    series = resolve_traffic_series("pdh", "demands")
    assert series is not None
    attachment = attach_traffic_stubs(plan, series, host_iface="eth1", render_frr=False)
    bgp = compile_bgp_plan(attachment.plan, "ibgp_rr")
    assert bgp is not None
    block = render_bgp_srl_block(bgp.nodes[0], bgp)
    assert block["autonomous-system"] == bgp.nodes[0].asn
    assert block["neighbor"]


def test_srl_ebgp_renders_rr_clients_and_passive_as_boundaries() -> None:
    plan = compile_isp_plan(IspConfig(topology="pdh", igp="ospf"))
    bgp = compile_bgp_plan(plan, "ebgp")
    assert bgp is not None
    scoped = scope_igp_to_bgp_as(plan, bgp)
    assert any(
        interface.passive for node in scoped.nodes for interface in node.interfaces
    )
    reflector = next(
        node
        for node in bgp.nodes
        if any(session.route_reflector_client for session in node.sessions)
    )
    block = render_bgp_srl_block(reflector, bgp)
    assert any("route-reflector" in neighbor for neighbor in block["neighbor"])


def test_clab_setup_script_is_serial(tmp_path) -> None:
    env = get_net_env_instance(
        "isp",
        backend="containerlab",
        topo="pdh",
        igp="isis",
        bgp_mode="ibgp_rr",
        device_profile="nokia_srlinux",
        lab_name="isp-unit",
    )
    env.runtime_workdir = tmp_path
    env._write_setup_script(lab_name="isp-unit")
    setup = (tmp_path / "setup.sh").read_text(encoding="utf-8")
    assert "configure_SRL" in setup
    assert "for VARIANT in" in setup
    assert ") &" not in setup
    assert "PIDS" not in setup
    assert "wait " not in setup


def test_host_ips_from_isp_inventory_data_plane() -> None:
    from nika.generator.traffic.sndlib_replay import host_ips_from_isp_inventory

    mapping = host_ips_from_isp_inventory(
        {
            "hosts": [
                {"host": "pc_a", "address": "10.254.1.2/30"},
                {"host": "pc_b", "address": "10.254.2.2/30"},
            ]
        }
    )
    assert mapping == {"pc_a": "10.254.1.2", "pc_b": "10.254.2.2"}


def test_get_net_env_instance_kathara_and_clab() -> None:
    k = get_net_env_instance("isp", backend="kathara", topo="pdh", device_profile="frr")
    assert k.backend == "kathara"
    assert k.device_profile == "frr"
    assert k.lab is not None

    c = get_net_env_instance(
        "isp",
        backend="containerlab",
        topo="pdh",
        device_profile="nokia_srlinux",
    )
    assert c.backend == "containerlab"
    assert c.device_profile == "nokia_srlinux"
    assert c.lab is None
    inv = c.inventory
    assert inv["links"][0]["endpoint_a"]["iface"].startswith("e1-")
    assert all(h.startswith("pc_") for h in c.hosts)
