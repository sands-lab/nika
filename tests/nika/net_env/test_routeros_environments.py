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
    assert frr.hosts
    assert set(frr.hosts) == set(native.hosts)
    assert set(native.native_routers) == {"hq_edge", "dc2_edge", "br1_edge", "br2_edge"}
    branch = native.deployment_configs["br1_edge"]
    # Business VRFs import overlay and other local business LANs; guest imports nothing.
    assert "/routing bgp vpn add name=vrf_corp vrf=vrf_corp" in branch
    assert "import.route-targets=65001:1 export.route-targets=65001:10" in branch
    assert "vrf=vrf_guest" not in branch
    assert "to-addresses=198.18.1.10" in branch
    # The primary WAN keeps container eth2, RouterOS ether4.
    assert native.primary_wan_interface() == frr.primary_wan_interface() == "eth2"
    assert "address=198.18.1.10/32 interface=ether4" in branch
    assert 'chain=POLICY-OUT rule="if (dst == 10.1.10.0/24) { accept; }"' in branch
    hub = native.deployment_configs["hq_edge"]
    assert "import.route-targets=65000:1,65000:20 export.route-targets=65000:10" in hub
    assert "import.route-targets=65000:1,65000:10 export.route-targets=65000:20" in hub
    assert "nexthop-choice=force-self" in hub
    assert "queue tree add name=wg_br1-ef parent=wg_br1" in hub
    # Every Site Edge translates guests to its own routed public address.
    assert "to-addresses=198.18.0.10" in hub and "198.18.1.10 comment" not in hub
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
    # FRR merges repeated route-map entries; the chain would keep both.
    repeated = (
        "ip prefix-list A seq 5 permit 10.0.0.0/24\n"
        "route-map M permit 10\n match ip address prefix-list A\n"
        "route-map M deny 10\n match ip address prefix-list A\n"
        "router bgp 1\n no bgp ebgp-requires-policy\n"
        " neighbor 10.0.0.0 remote-as 2\n neighbor 10.0.0.0 route-map M out\n"
    )
    with pytest.raises(ValueError, match="repeats a sequence number"):
        translate_frr_router("r1", repeated, startup)


@pytest.mark.unit
def test_solved_parameters_translate_to_routeros_routers():
    from nika.net_env.net_env_pool import get_net_env_instance

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
        "dc_clos", topo_size="s", device_profile="routeros", routing_parameters=exports
    ).deployment_configs["leaf_router_0_0"]
    # First exact-prefix match applies; unmatched routes are rejected.
    rules = [line for line in leaf.splitlines() if "chain=NC-OUT-0 " in line]
    assert rules == [
        '/routing filter rule add chain=NC-OUT-0 rule="if (dst == 10.0.0.0/24 && dst-len == 24) { accept; }"',
        '/routing filter rule add chain=NC-OUT-0 rule="if (dst == 10.0.1.0/24 && dst-len == 24) { reject; }"',
        '/routing filter rule add chain=NC-OUT-0 rule="reject;"',
    ]
    assert (
        "remote.address=172.16.0.4 " in leaf and "output.filter-chain=NC-OUT-0" in leaf
    )
    core = get_net_env_instance(
        "campus_lan",
        topo_size="s",
        device_profile="routeros",
        routing_parameters={
            "ospf_costs": [{"node": "router_core_1", "interface": "eth0", "cost": 7}]
        },
    ).deployment_configs["router_core_1"]
    assert "area=area-0.0.0.0 networks=172.16.0.0/31 cost=7" in core
    edge = get_net_env_instance(
        "enterprise_branch",
        topo_size="s",
        device_profile="routeros",
        routing_parameters={
            "import_preferences": [
                {
                    "node": "br1_edge",
                    "interface": "wg_hq",
                    "prefix": "10.0.10.0/24",
                    "permit": True,
                    "local_preference": 300,
                }
            ]
        },
    ).deployment_configs["br1_edge"]
    chain = [line for line in edge.splitlines() if "chain=POLICY-IN-wg_hq " in line]
    assert chain == [
        '/routing filter rule add chain=POLICY-IN-wg_hq rule="if (dst == 10.0.10.0/24 && dst-len == 24) { set bgp-local-pref 300; accept; }"',
        '/routing filter rule add chain=POLICY-IN-wg_hq rule="if (dst in 10.0.0.0/8 && dst-len <= 24) { set bgp-local-pref 200; accept; }"',
        '/routing filter rule add chain=POLICY-IN-wg_hq rule="reject;"',
    ]


@pytest.mark.unit
def test_mixed_platform_runtime_report_keeps_each_row(monkeypatch):
    from types import SimpleNamespace

    from nika.net_env import native_routers
    from pathlib import Path

    from nika.net_env.contract import ValidationReport, ValidationResult
    from nika.net_env.synthesis.requirements import resolve_routing_requirements
    from nika.workflows.validation.scenario_spec import ScenarioExperiment

    spec = ScenarioExperiment.model_validate_json(
        (
            Path(__file__).resolve().parents[3]
            / "experiment/scenario_validation/enterprise-routeros.json"
        ).read_text()
    )
    env = _build("enterprise_branch", "routeros")
    contract = resolve_routing_requirements(spec, env).runtime_requirements

    def platform_report(name):
        def verify(runtime, contract, plan=None):
            return ValidationReport.from_results(
                contract,
                name,
                [
                    ValidationResult(
                        intent=i.id,
                        verifier=name,
                        status="passed",
                        supported=True,
                        evidence={},
                        duration_ms=0,
                    )
                    for i in contract.intents
                ],
            )

        return verify

    monkeypatch.setattr(
        native_routers,
        "native_platform",
        lambda profile: SimpleNamespace(
            validation=SimpleNamespace(
                verify_contract=platform_report("routeros-runtime"),
                node=lambda name, config: native_routers.NativeRouterNode(name, ()),
            )
        ),
    )
    fallback = platform_report("frr-runtime")
    verify = native_routers.verify_native_router_contract(
        env, lambda runtime, contract, plan: fallback(runtime, contract)
    )
    report = verify(None, contract=contract)
    platforms = {row.evidence["platform_verifier"] for row in report.results}
    assert platforms == {"frr-runtime", "routeros-runtime"}
    assert report.status == "passed"


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
    ops = router_fault_ops(problem("routeros"), leaf)
    assert not ops.supports("stop_routing_process")


@pytest.mark.unit
def test_routeros_connection_match_accepts_split_and_joined_ports():
    from nika.net_env.utils.routeros.fault_ops import _address_match

    # RouterOS 7.21 prints the port apart; older releases append it.
    assert _address_match("reply-dst-address", "198.18.1.10") == (
        'reply-dst-address="198.18.1.10" or reply-dst-address~"^198.18.1.10:"'
    )
