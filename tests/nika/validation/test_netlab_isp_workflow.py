"""Upstream construction must satisfy the original static and native predicates."""

from copy import deepcopy
from importlib.util import find_spec

import pytest

from tests.support.scenario_validation import assert_scenario_acceptance

from nika.workflows.validation.benchmark_routing import (
    build_benchmark_isp_experiment,
    isp_catalog_profiles,
)


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
