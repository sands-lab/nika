"""E2E validation helpers for selected / catalog benchmark cases."""

from __future__ import annotations

import functools

from nika.net_env.isp.identity import (
    is_isp_base_topology,
    isp_topo_from_scenario,
)

# Memory a Containerlab ISP lab may use: what 11 SR Linux routers and their
# stubs need, as Containerlab ISP labs of 10-11 SR Linux routers peak at
# 14.2-15.7 GiB on a 16 GiB host (12-router graphs ran out of memory).
CONTAINERLAB_ISP_MAX_SRL_ROUTERS = 11
# Memory of one ISP edge stub host (the Firecracker sizing figure).
_STUB_MEMORY_MIB = 64


def _router_memory_mib(device_profile: str) -> int:
    from importlib import import_module

    return import_module(
        f"nika.net_env.utils.{device_profile}.common"
    ).ROUTER_MEMORY_MIB


def containerlab_isp_memory_budget_mib() -> int:
    return CONTAINERLAB_ISP_MAX_SRL_ROUTERS * (
        _router_memory_mib("nokia_srlinux") + _STUB_MEMORY_MIB
    )


def containerlab_isp_max_routers(device_profile: str | None = None) -> int:
    """Largest ISP graph whose routers, one stub each, fit the memory budget.

    ``device_profile`` defaults to the Containerlab default platform (SR Linux).
    """
    from nika.net_env.isp.profiles import default_device_profile

    profile = device_profile or default_device_profile("containerlab")
    per_router = _router_memory_mib(profile) + _STUB_MEMORY_MIB
    return containerlab_isp_memory_budget_mib() // per_router


@functools.cache
def _isp_router_count(topo: str) -> int:
    from nika.topology.catalog import load_topology

    return len(load_topology(topo).nodes)


def isp_containerlab_node_count(scenario: str) -> int | None:
    """Return the router count of an ISP base scenario, else None."""
    if not is_isp_base_topology(scenario):
        return None
    return _isp_router_count(isp_topo_from_scenario(scenario))


def containerlab_isp_supported(
    scenario: str, device_profile: str | None = None
) -> bool:
    """Whether an ISP base topology's routers on ``device_profile`` fit a Containerlab host."""
    nodes = isp_containerlab_node_count(scenario)
    return nodes is not None and nodes <= containerlab_isp_max_routers(device_profile)
