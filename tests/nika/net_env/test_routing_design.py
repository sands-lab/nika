"""Recorded routing designs match the configuration NIKA deploys."""

import re
from ipaddress import IPv4Interface

import pytest

from nika.net_env.native_routers import router_sketches
from nika.net_env.net_env_pool import get_net_env_instance
from nika.validation.batfish.kathara import startup_addresses


@pytest.mark.contract
@pytest.mark.parametrize("scenario", ["dc_clos", "campus_lan", "enterprise_branch"])
@pytest.mark.parametrize("size", ["s", "m"])
def test_design_matches_deployed_addressing_and_bgp(scenario, size):
    env = get_net_env_instance(scenario, topo_size=size)
    sketches = router_sketches(env)
    assert set(env.routing_design.routers) == set(sketches)
    for name, router in env.routing_design.routers.items():
        config, startup = sketches[name]
        deployed = {
            (str(IPv4Interface(a)), i)
            for a, i in startup_addresses(startup)
            # NAT pool aliases are not routing design.
            if IPv4Interface(a).network.prefixlen < 32
        }
        # Tunnel interfaces take their addresses from the routing configuration.
        recorded = {
            (i.address, i.name)
            for i in router.interfaces
            if not (i.link or "").startswith("tunnel_")
        }
        assert recorded == deployed
        neighbors = set(
            re.findall(r"^\s*neighbor (\S+) remote-as ([0-9]+)$", config, re.M)
        )
        assert {(n.address, str(n.remote_as)) for n in router.neighbors} == neighbors
    networks = {
        prefix: name
        for name, (config, _) in sketches.items()
        for prefix in re.findall(r"^\s*network ([0-9./]+)$", config, re.M)
    }
    assert env.routing_design.origin_prefixes() == networks


@pytest.mark.contract
def test_retained_capabilities_name_the_generator_limitation():
    from nika.net_env.synthesis.netlab_kathara import retained_capabilities

    env = get_net_env_instance("enterprise_branch", topo_size="s")
    (retained,) = retained_capabilities(env)
    assert retained["capability"] == "default-to-business VRF imports"
    assert "netlab" in retained["reason"]
