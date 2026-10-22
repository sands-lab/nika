"""IOS XR device profiles for Ethernet scenarios (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.net_env_pool import get_net_env_instance, scenario_device_profiles
from nika.net_env.utils.iosxr.frr import translate_frr_router
from nika.workflows.env.start import _resolve_isp_kwargs

pytestmark = pytest.mark.unit


def _profile_kwargs(scenario: str, profile: str | None) -> dict:
    return _resolve_isp_kwargs(
        scenario,
        topo=None,
        igp=None,
        metric_strategy=None,
        constant_metric=None,
        bgp_mode=None,
        device_profile=profile,
    )


def test_scenarios_declare_native_profiles() -> None:
    assert scenario_device_profiles("dc_clos") == ("iosxr", "routeros")
    assert _profile_kwargs("dc_clos", "iosxr") == {"device_profile": "iosxr"}
    # frr selects the unchanged scenario default.
    assert _profile_kwargs("dc_clos", "frr") == {}
    assert _profile_kwargs("dc_clos", None) == {}
    with pytest.raises(ValueError, match="does not support device_profile"):
        _profile_kwargs("sdn_l3_clos", "iosxr")


@pytest.mark.parametrize(
    ("scenario", "native", "kept"),
    [
        # Bridged gateways with DHCP relay and WireGuard/NAT edges stay FRR.
        (
            "campus_lan",
            {"router_core_1", "router_core_2", "router_core_3"},
            "router_dist_1_1",
        ),
        ("enterprise_branch", {"isp1_core", "isp2_core"}, "hq_edge"),
    ],
)
def test_iosxr_replaces_only_declared_roles(scenario, native, kept) -> None:
    frr = get_net_env_instance(scenario, topo_size="s")
    xr = get_net_env_instance(scenario, topo_size="s", device_profile="iosxr")
    assert set(xr.native_routers) == native
    assert set(xr.lab.machines) == set(frr.lab.machines)
    for name, machine in xr.lab.machines.items():
        original = frr.lab.machines[name]
        assert [i.link.name for i in machine.interfaces.values()] == [
            i.link.name for i in original.interfaces.values()
        ]
        if name not in native:
            assert machine.meta == original.meta
            startup = f"{name}.startup"
            if frr.lab.fs.exists(startup):
                assert xr.lab.fs.readtext(startup) == frr.lab.fs.readtext(startup)
    assert "iosxr" in xr.machine_identities[min(native)].capabilities
    assert "frr" in xr.machine_identities[kept].capabilities


def test_dc_clos_keeps_its_design_on_iosxr() -> None:
    frr = get_net_env_instance("dc_clos", topo_size="s")
    xr = get_net_env_instance("dc_clos", topo_size="s", device_profile="iosxr")
    assert type(xr) is type(frr)
    assert set(xr.lab.machines) == set(frr.lab.machines)
    assert xr.bgp_neighbors == frr.bgp_neighbors
    assert xr.routers == frr.routers
    for name in xr.routers:
        identity = xr.machine_identities[name]
        assert "iosxr" in identity.capabilities and "frr" not in identity.capabilities
        config = xr.deployment_configs[name]
        assert config.count(" neighbor ") == len(frr.bgp_neighbors[name])
        # FRR accepts all eBGP routes without ebgp-requires-policy; XR needs
        # an explicit pass policy for the same behavior.
        assert config.count("route-policy PASS in") == len(frr.bgp_neighbors[name])
        assert "maximum-paths ebgp 32" in config
    leaf = xr.deployment_configs["leaf_router_0_0"]
    assert "network 10.0.0.0/24" in leaf
    with pytest.raises(ValueError, match="supports size s only"):
        get_net_env_instance("dc_clos", topo_size="m", device_profile="iosxr")


def test_translation_rejects_unmodeled_router_behavior() -> None:
    startup = "ip addr add 10.0.0.1/24 dev eth0\nservice frr start\n"
    with pytest.raises(ValueError, match="Unsupported FRR statement"):
        translate_frr_router("r1", "router ospf\n redistribute connected\n", startup)
    with pytest.raises(ValueError, match="Unsupported IOS XR router startup"):
        translate_frr_router("r1", "router bgp 1\n", startup + "brctl addbr br0\n")


def test_translation_keeps_areas_static_routes_and_underlay_delay() -> None:
    campus = get_net_env_instance("campus_lan", topo_size="s", device_profile="iosxr")
    core = campus.deployment_configs["router_core_1"]
    assert core.index(" area 0") < core.index(" area 1")
    enterprise = get_net_env_instance(
        "enterprise_branch", topo_size="s", device_profile="iosxr"
    )
    provider = enterprise.deployment_configs["isp1_core"]
    assert "  198.18.1.10/32 100.64.0.17" in provider
    # XR traffic still passes the container qdiscs, but the XRd image has no
    # tc: the provider's WAN delay is applied from the router's namespace.
    assert (
        "tc qdisc replace dev eth0 root netem delay 20ms"
        in enterprise.native_netns_commands["isp1_core"]
    )
    assert "netem" not in enterprise.lab.fs.readtext("isp1_core.startup")


def test_solved_parameters_translate_with_the_router_design() -> None:
    exports = {
        "bgp_exports": [
            {
                "node": "leaf_router_0_0",
                "neighbor": "172.16.0.4",
                "terms": [
                    {"prefix": "10.0.0.0/24", "permit": True},
                    {"prefix": "10.0.1.0/24", "permit": False},
                ],
            }
        ]
    }
    leaf = get_net_env_instance(
        "dc_clos", topo_size="s", device_profile="iosxr", routing_parameters=exports
    ).deployment_configs["leaf_router_0_0"]
    # First matching route-map entry applies; unmatched routes are denied.
    assert (
        "route-policy NC-OUT-0\n"
        "  if destination in NC-0-1 then\n    pass\n"
        "  elseif destination in NC-0-2 then\n    drop\n"
        "  else\n    drop\n  endif\nend-policy"
    ) in leaf
    assert "prefix-set NC-0-1\n  10.0.0.0/24\nend-set" in leaf
    peer = leaf.split(" neighbor 172.16.0.4\n", 1)[1].split(" !\n", 2)[0]
    assert "route-policy PASS in" in peer and "route-policy NC-OUT-0 out" in peer
    costs = {"ospf_costs": [{"node": "router_core_1", "interface": "eth0", "cost": 7}]}
    core = get_net_env_instance(
        "campus_lan", topo_size="s", device_profile="iosxr", routing_parameters=costs
    ).deployment_configs["router_core_1"]
    assert "  interface GigabitEthernet0/0/0/0\n   cost 7\n  !" in core


def test_mixed_snapshot_exports_native_and_frr_routers(tmp_path) -> None:
    import json

    from nika.net_env.contract import ValidationContract
    from nika.validation.batfish.kathara import build_kathara_snapshot

    env = get_net_env_instance("campus_lan", topo_size="s", device_profile="iosxr")
    contract = ValidationContract.model_validate(
        {
            "scenario": "campus_lan",
            "contract_id": "mixed",
            "design_source": {"basis": "test"},
            "intents": [],
        }
    )
    snapshot = build_kathara_snapshot(
        env, contract, tmp_path, shutdown=[("router_core_1", "eth0")]
    )
    configs = snapshot.path / "configs"
    core = (configs / "router_core_1.cfg").read_text()
    assert core.startswith("!! IOS XR Configuration")
    disabled = core.split("interface GigabitEthernet0/0/0/0\n", 1)[1].split("!", 1)[0]
    assert " shutdown" in disabled and "no shutdown" not in disabled
    assert "frr version" in (configs / "router_dist_1_1.cfg").read_text()
    assert snapshot.metadata["interface_aliases"]["router_core_1"] == {
        "GigabitEthernet0/0/0/0": "eth0",
        "GigabitEthernet0/0/0/1": "eth1",
        "GigabitEthernet0/0/0/2": "eth2",
    }
    edges = json.loads((snapshot.path / "batfish/layer1_topology.json").read_text())
    names = {
        (end["hostname"], end["interfaceName"])
        for edge in edges["edges"]
        for end in (edge["interface1"], edge["interface2"])
    }
    assert ("router_core_1", "GigabitEthernet0/0/0/2") in names
    assert ("router_dist_1_1", "eth0") in names


def test_netlab_sketch_runs_eligible_routers_as_xr() -> None:
    from nika.net_env.synthesis.netlab_kathara import kathara_topology
    from nika.net_env.utils.iosxr.frr import retained_netlab_fragment

    env = get_net_env_instance("campus_lan", topo_size="s", device_profile="iosxr")
    topology, _ = kathara_topology(env)
    assert topology["nodes"]["router_core_1"]["device"] == "iosxr"
    assert "device" not in topology["nodes"]["router_dist_1_1"]
    names = {
        (i["node"], i["ifname"])
        for link in topology["links"]
        for i in link["interfaces"]
    }
    assert ("router_core_1", "GigabitEthernet0/0/0/0") in names
    # netlab XR templates omit Linux-owned static routes and the FRR eBGP
    # defaults; NIKA retains them as native statements.
    provider = get_net_env_instance(
        "enterprise_branch", topo_size="s", device_profile="iosxr"
    ).native_sources["isp1_core"]
    assert "  198.18.1.10/32 100.64.0.17" in retained_netlab_fragment(
        "isp1_core", *provider
    )
    leaf = get_net_env_instance(
        "dc_clos", topo_size="s", device_profile="iosxr"
    ).native_sources["leaf_router_0_0"]
    fragment = retained_netlab_fragment("leaf_router_0_0", *leaf)
    assert "maximum-paths ebgp 32" in fragment
    assert "route-policy PASS in" in fragment
