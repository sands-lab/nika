"""get_net_env_instance must construct labs that omit backend from __init__."""

from __future__ import annotations

from nika.net_env.net_env_pool import get_net_env_instance


def test_dc_clos_service_accepts_backend_kwarg() -> None:
    env = get_net_env_instance("dc_clos_service", backend="kathara", topo_size="s")
    assert env.backend == "kathara"
    assert env.lab is not None


def test_simple_bgp_forwards_backend() -> None:
    env = get_net_env_instance("simple_bgp", backend="kathara")
    assert env.backend == "kathara"
    assert env.lab is not None
