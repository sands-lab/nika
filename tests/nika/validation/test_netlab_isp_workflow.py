"""Upstream construction must satisfy the original static and native predicates."""

from copy import deepcopy
from importlib.util import find_spec

import pytest

from tests.support.scenario_validation import assert_scenario_acceptance

from nika.workflows.validation.benchmark_routing import (
    build_benchmark_isp_experiment,
    isp_catalog_profiles,
)


@pytest.mark.unit
def test_frr_netlab_fault_bindings_preserve_original_edits():
    from nika.workflows.validation.netlab import (
        _bind_frr_failure_edit,
        _frr_bgp_network_line,
        _frr_network,
    )

    config = (
        "router bgp 65000\n!\nrouter bgp 65000\n address-family ipv4\n"
        "  neighbor 192.0.2.1 activate\n"
    )
    old, new = _bind_frr_failure_edit(
        config, "router bgp 65000\n", "router bgp 65600\n"
    )
    assert old == config
    assert new.count("router bgp 65600\n") == 2

    config = (
        "interface eth0\n ip address 10.0.0.1/24\n!\n"
        "interface eth0\n ip ospf area 0.0.0.0\n!\n"
    )
    old = "router ospf\n network 10.0.0.0 0.0.0.255 area 0\n"
    new = "router ospf\n network 10.0.0.0 0.0.0.255 area 66\n"
    actual_old, actual_new = _bind_frr_failure_edit(config, old, new)
    assert actual_old == config
    assert "ip ospf area 0.0.0.66" in actual_new
    assert _frr_network("10.0.0.0 0.0.0.255").prefixlen == 24

    config = (
        "router bgp 65000\n address-family ipv4 unicast\n"
        "  network 198.51.100.0/24\n exit-address-family\n"
    )
    assert _frr_bgp_network_line(config, "  network 198.51.100.0/24") == (
        "  network 198.51.100.0/24\n"
    )

    config = (
        " neighbor 192.0.2.1 route-map BGP-OUT-ipv4 out\n"
        "route-map BGP-OUT-ipv4 deny 5\n match ip address prefix-list EXPORT-DENY\n"
    )
    actual_old, actual_new = _bind_frr_failure_edit(
        config, "route-map BGP-OUT deny 5", "route-map BGP-OUT permit 5"
    )
    assert actual_old == "route-map BGP-OUT-ipv4 deny 5"
    assert actual_new == "route-map BGP-OUT-ipv4 permit 5"


@pytest.mark.e2e
@pytest.mark.parametrize(
    "vendor,topology,igp,bgp_mode",
    [
        (vendor, topology, "ospf", mode)
        for vendor in ("frr", "iosxr")
        for topology, mode in (
            ("arpanet196912", "none"),
            ("arpanet196912", "ibgp_rr"),
            ("dataxchange", "ebgp"),
        )
    ]
    + [
        ("frr", topology, "isis", mode)
        for topology in ("arpanet196912", "dataxchange", "abilene")
        for mode in ("none", "ibgp_rr")
    ]
    + [
        ("routeros", topology, "ospf", mode)
        for topology, mode in (
            ("arpanet196912", "none"),
            ("arpanet196912", "ibgp_rr"),
            ("dataxchange", "ebgp"),
        )
    ]
    + [
        ("nokia_srlinux", "dfn-bwin", "isis", "ibgp_rr"),
        ("nokia_srlinux", "abilene", "ospf", "none"),
        ("nokia_srlinux", "abilene", "isis", "ebgp"),
    ]
    + [("frr", topology, "isis", "ebgp") for topology in ("dataxchange", "abilene")]
    + [
        ("routeros", "dataxchange", "ospf", "ibgp_rr"),
        ("frr", "arpanet19706", "isis", "ibgp_rr"),
    ],
)
def test_netlab_synthesis_execution_and_recovery(
    vendor, topology, igp, bgp_mode, tmp_path, netcomplete_image, batfish_verifier
):
    if find_spec("netsim") is None:
        pytest.skip("Install the netlab extra for upstream construction tests")
    import docker
    from docker.errors import ImageNotFound
    from nika.net_env.utils.iosxr.common import IMAGE
    from nika.workflows.validation.scenario import validate_scenario_design
    from nika.workflows.validation.scenario_build import load_scenario_build
    from nika.net_env.net_env_pool import get_net_env_instance

    if vendor == "iosxr":
        client = docker.from_env()
        try:
            try:
                client.images.get(IMAGE)
            except ImageNotFound:
                pytest.skip("Install the licensed XRd image first")
        finally:
            client.close()
    profile = deepcopy(
        next(
            p
            for p in isp_catalog_profiles().values()
            if p["scenario"] == f"isp_{topology}"
        )
    )
    profile["deployment"].update(igp=igp, bgp_mode=bgp_mode, device_profile=vendor)
    if vendor == "nokia_srlinux":
        profile["deployment"]["backend"] = "containerlab"
    spec = build_benchmark_isp_experiment(profile)
    summary = validate_scenario_design(
        spec,
        tmp_path,
        runtime=True,
        image=netcomplete_image,
        config_generator="netlab",
        batfish_verifier=batfish_verifier,
    )
    assert_scenario_acceptance(spec, summary, tmp_path, runtime=True)
    assert summary["configuration"]["generator"] == "netlab"
    assert summary["configuration"]["version"] == "26.10"
    if vendor == "nokia_srlinux" or (vendor == "routeros" and bgp_mode != "none"):
        assert summary["configuration"]["retained"]
    # Ordinary replay consumes concrete accepted configurations, without tools.
    kwargs = load_scenario_build(tmp_path)
    kwargs.pop("static_validation")
    env = get_net_env_instance(kwargs.pop("scenario"), **kwargs)
    if vendor == "nokia_srlinux":
        import yaml

        assert env.routing_documents == {
            p.stem: yaml.safe_load(p.read_text())
            for p in (tmp_path / "candidate-configs").glob("*.yaml")
        }
    else:
        assert env.deployment_configs == {
            p.stem: p.read_text()
            for p in (tmp_path / "candidate-configs").glob("*.conf")
        }
