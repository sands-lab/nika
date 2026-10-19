"""Concrete ISP candidates preserve routing semantics in vendor snapshots."""

from dataclasses import replace
from ipaddress import IPv4Interface

import pytest

from nika.net_env.isp.bgp.plan import compile_bgp_plan, scope_igp_to_bgp_as
from nika.net_env.isp.bgp.policy import BgpPolicyTerm
from nika.net_env.isp.igp.config import IspConfig
from nika.net_env.isp.igp.plan import compile_isp_plan
from nika.net_env.isp.routeros import render_routeros_isp, routeros_interface
from nika.validation.batfish.routeros_isp import project_routeros_isp


@pytest.mark.contract
@pytest.mark.parametrize("mode", ["none", "ibgp_rr", "ebgp"])
def test_routeros_isp_projects_concrete_sessions_and_isolated_origins(mode):
    plan = compile_isp_plan(
        IspConfig(topology="abilene", igp="ospf", first_interface_index=1)
    )
    bgp = compile_bgp_plan(plan, mode)
    plan = scope_igp_to_bgp_as(plan, bgp)
    configs = render_routeros_isp(plan, bgp)
    for node in plan.nodes:
        projected = project_routeros_isp(node.device_name, configs[node.device_name])
        ospf = projected.split("router ospf\n", 1)[1].split("\n!", 1)[0]
        for interface in node.interfaces:
            name = interface.name
            assert (f" passive-interface {name}\n" in ospf + "\n") == interface.passive
            assert f" network {interface.subnet} area 0.0.0.0" in ospf
        if bgp is None:
            continue
        bgp_node = next(n for n in bgp.nodes if n.device_name == node.device_name)
        for origin in bgp_node.originated:
            assert f" network {origin.ping_address}/32" not in ospf
            assert f"  network {origin.prefix}\n" in projected
        for session in bgp_node.sessions:
            assert (
                f" neighbor {session.remote_ip} remote-as {session.remote_asn}"
                in projected
            )
            assert (
                f"  neighbor {session.remote_ip} route-reflector-client" in projected
            ) == session.route_reflector_client
        if bgp_node.cluster_id:
            assert f" bgp cluster-id {bgp_node.cluster_id}" in projected
        if mode == "ebgp":
            assert "next-hop-self" not in projected
            if any(s.route_reflector_client for s in bgp_node.sessions):
                with pytest.raises(ValueError, match="next-hop mode"):
                    project_routeros_isp(
                        node.device_name,
                        configs[node.device_name].replace(
                            "nexthop-choice=default", "nexthop-choice=force-self"
                        ),
                    )


@pytest.mark.contract
def test_routeros_isp_projection_reads_mutated_cost_and_import_policy():
    plan = compile_isp_plan(
        IspConfig(topology="abilene", igp="ospf", first_interface_index=1)
    )
    bgp = compile_bgp_plan(plan, "ebgp")
    node = next(n for n in bgp.nodes if n.sessions and n.originated)
    peer = node.sessions[0].remote_ip
    policy = BgpPolicyTerm(
        prefix=node.originated[0].prefix, permit=True, local_preference=231
    )
    node = replace(node, import_policies=((peer, (policy,)),))
    bgp = replace(
        bgp,
        nodes=tuple(
            node if n.device_name == node.device_name else n for n in bgp.nodes
        ),
    )
    plan = scope_igp_to_bgp_as(plan, bgp)
    config = render_routeros_isp(plan, bgp)[node.device_name]
    projected = project_routeros_isp(node.device_name, config)
    assert f"neighbor {peer} route-map NC-IN-0 in" in projected
    assert " set local-preference 231" in projected
    assert " set local-preference 232" in project_routeros_isp(
        node.device_name,
        config.replace("set bgp-local-pref 231", "set bgp-local-pref 232"),
    )
    assert " ip ospf cost 19" in project_routeros_isp(
        node.device_name, config.replace("cost=10", "cost=19")
    )
    name = (
        next(a for a in plan.nodes if a.device_name == node.device_name)
        .interfaces[0]
        .name
    )
    physical = routeros_interface(name)
    disabled = project_routeros_isp(
        node.device_name, config, shutdown=((node.device_name, physical),)
    )
    assert " shutdown" in disabled.split(f"interface {name}\n", 1)[1].split("!", 1)[0]
    # A nonmatching business prefix still follows the original allow-list.
    assert "route-map NC-IN-0 permit" in projected
    assert (
        str(IPv4Interface(f"{node.originated[0].ping_address}/24").network) in projected
    )


@pytest.mark.contract
@pytest.mark.parametrize(
    "statement",
    [
        "/ip firewall filter add chain=forward action=drop",
        "/routing ospf interface-template add area=backbone interfaces=ether1 networks=0.0.0.0/0 type=nbma passive=no cost=10 disabled=yes",
        '/routing filter rule add chain=UNKNOWN rule="set bgp-path-prepend 3; accept;"',
    ],
)
def test_routeros_isp_projection_rejects_unmodeled_forwarding(statement):
    plan = compile_isp_plan(
        IspConfig(topology="abilene", igp="ospf", first_interface_index=1)
    )
    bgp = compile_bgp_plan(plan, "ibgp_rr")
    node, config = next(iter(render_routeros_isp(plan, bgp).items()))
    with pytest.raises(ValueError, match="Unsupported"):
        project_routeros_isp(node, config + statement + "\n")


@pytest.mark.contract
def test_routeros_projection_preserves_names_that_resemble_physical_ports():
    plan = compile_isp_plan(
        IspConfig(topology="abilene", igp="ospf", first_interface_index=1)
    )
    node, config = next(iter(render_routeros_isp(plan).items()))
    config = config.replace(
        f"/system identity set name={node}", "/system identity set name=ether2"
    )
    projected = project_routeros_isp("ether2", config)
    assert projected.startswith("hostname ether2\n")
    assert "interface eth1\n" in projected


@pytest.mark.contract
@pytest.mark.parametrize("address", ["10.0.0.0/31", "10.0.0.0 255.255.255.254"])
def test_iosxr_concrete_addresses_accept_prefix_and_netmask_notation(address):
    from nika.validation.batfish.vendor_bgp import vendor_addresses

    assert vendor_addresses(
        f"hostname r1\ninterface GigabitEthernet0/0/0/0\n ipv4 address {address}\n!\n",
        "iosxr",
    ) == [("10.0.0.0/31", "GigabitEthernet0/0/0/0")]


@pytest.mark.contract
@pytest.mark.parametrize("enabled", [False, True])
def test_frr_passive_import_reads_concrete_candidate_not_original_plan(tmp_path, enabled):
    import re

    from nika.problems.rca.inventory import load_offline_net_env
    from nika.validation.batfish.kathara import build_kathara_snapshot

    env = load_offline_net_env("isp_arpanet196912", igp="ospf", bgp_mode="none")
    node = env.plan.nodes[0]
    iface = node.interfaces[0]
    assert not iface.passive
    configs = dict(env.deployment_configs)
    config = re.sub(
        r"^ passive-interface .+\n", "", configs[node.device_name], flags=re.M
    )
    command = "ip ospf passive" if enabled else "no ip ospf passive"
    configs[node.device_name] = config + f"\ninterface {iface.name}\n {command}\n!\n"
    snapshot = build_kathara_snapshot(
        env, env.validation_contract, tmp_path, configs=configs
    )
    imported = (snapshot.path / "configs" / f"{node.device_name}.cfg").read_text()
    assert (f" passive-interface {iface.name}\n" in imported) == enabled
    assert "ip ospf passive" not in imported
