"""Upstream construction must satisfy the original static and native predicates."""

from copy import deepcopy
from importlib.util import find_spec

import pytest

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
    ],
)
def test_netlab_synthesis_execution_and_recovery(
    vendor, topology, igp, bgp_mode, tmp_path, netcomplete_image
):
    if find_spec("netsim") is None:
        pytest.skip("Install the netlab extra for upstream construction tests")
    import docker
    from docker.errors import ImageNotFound
    from nika.net_env.utils.iosxr.common import IMAGE
    from nika.workflows.validation.routing import validate_routing_design
    from nika.workflows.validation.routing_build import load_routing_build
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
    spec = build_benchmark_isp_experiment(profile)
    summary = validate_routing_design(
        spec, tmp_path, runtime=True, image=netcomplete_image, config_generator="netlab"
    )
    assert summary["status"] == "passed"
    assert summary["configuration"] == {"generator": "netlab", "version": "26.10"}
    assert (
        summary["runtime"]["backbone-link-down-recovered"]
        == summary["runtime"]["healthy"]
    )
    # Ordinary replay consumes concrete accepted configurations, without tools.
    kwargs = load_routing_build(tmp_path)
    kwargs.pop("static_validation")
    env = get_net_env_instance(kwargs.pop("scenario"), **kwargs)
    assert env.deployment_configs == {
        p.stem: p.read_text() for p in (tmp_path / "candidate-configs").glob("*.conf")
    }
