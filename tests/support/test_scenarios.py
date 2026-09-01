"""Register test-only network scenarios for integration tests."""

from nika.net_env.net_env_pool import NetEnvSpec, _NET_ENV_SPECS


def register_test_scenarios() -> None:
    if "simple_bgp" in _NET_ENV_SPECS:
        return
    _NET_ENV_SPECS["simple_bgp"] = NetEnvSpec(
        lab_name="simple_bgp",
        module="tests.support.simple_bgp.lab",
        class_name="SimpleBGP",
        tags=("arp", "link", "mac", "bgp", "icmp", "frr", "pc"),
        supported_backends=("kathara",),
    )
