"""Concrete IOS XR and FRR ISP candidates in Batfish snapshots."""

import pytest


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
def test_frr_passive_import_reads_concrete_candidate_not_original_plan(
    tmp_path, enabled
):
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
