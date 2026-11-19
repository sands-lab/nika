"""Address discovery for probes across native and Linux routers."""

from types import SimpleNamespace

import pytest

from nika.problems.rca.inventory import load_offline_net_env
from nika.validation.symptom.combinations import ProbeContext, _far_end_address

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("profile", "cidr", "expected"),
    [("routeros", "172.16.0.1/31", "172.16.0.0"), ("frr", "10.0.0.1/30", "10.0.0.2")],
)
def test_pmtud_peer_address_uses_the_routers_address_view(profile, cidr, expected):
    env = load_offline_net_env("dc_clos", "s", device_profile=profile)

    def execute(node, command, **kwargs):
        assert node == "leaf_1"
        if profile == "routeros":
            assert "/ip/address/print terse where interface=" in command
            return f"0 address={cidr} interface=ether5\n"
        assert "ip -4 -o addr show dev" in command
        return f"4: eth3 inet {cidr} scope global eth3\n"

    runtime = SimpleNamespace(exec=execute, _net_env=env)
    problem = SimpleNamespace(net_env=env, runtime=runtime)
    ctx = ProbeContext(runtime, env, {}, {"mtu_mismatch": problem})
    assert _far_end_address(ctx, "leaf_1", "eth3") == expected
