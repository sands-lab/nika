"""Shared initialization for problem classes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nika.net_env.net_env_pool import get_net_env_instance
from nika.runtime.base import LabRuntime
from nika.runtime.factory import runtime_for_net_env

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase


def init_problem(
    scenario_name: str | None, **kwargs: Any
) -> tuple[NetworkEnvBase, LabRuntime]:
    """Resolve network environment and backend-neutral runtime for a problem."""
    if scenario_name is not None:
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(scenario_name):
            # Session metadata records the fixed ISP scale/topology for benchmark
            # sampling, but the canonical scenario ID owns deployment identity.
            kwargs.pop("topo_size", None)
            kwargs.pop("topo", None)
    net_env = get_net_env_instance(scenario_name, **kwargs)
    runtime = kwargs.get("runtime")
    if runtime is None:
        runtime = runtime_for_net_env(net_env)
        net_env.runtime = runtime
    return net_env, runtime
