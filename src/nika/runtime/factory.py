"""Runtime factory helpers for backend resolution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nika.runtime.base import LabRuntime
from nika.utils.dependencies import raise_missing_extra, require_backend_extra
from nika.runtime.meta import meta_get, meta_lab_name, meta_path

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase


def resolve_backend(meta: dict[str, Any] | Any) -> str:
    """Return session backend; infer from scenario when metadata is missing."""
    backend = meta_get(meta, "backend")
    scenario_params = meta_get(meta, "scenario_params") or {}
    scenario_name = meta_get(meta, "scenario_name")

    if backend:
        return str(backend)
    if isinstance(scenario_params, dict) and scenario_params.get("backend"):
        return str(scenario_params["backend"])

    if scenario_name:
        from nika.net_env.net_env_pool import scenario_supported_backends

        try:
            supported = scenario_supported_backends(str(scenario_name))
        except ValueError:
            pass
        else:
            if len(supported) == 1:
                return supported[0]

    return "kathara"


def _containerlab_runtime(
    *,
    lab_name: str,
    topology_file: Any,
    runtime_workdir: Any = None,
) -> LabRuntime:
    require_backend_extra("containerlab")
    try:
        from nika.runtime.containerlab.runtime import ContainerlabRuntime
    except ImportError as exc:
        raise_missing_extra("containerlab", cause=exc)
    return ContainerlabRuntime(
        lab_name=lab_name,
        topology_file=topology_file,
        runtime_workdir=runtime_workdir,
    )


def _kathara_runtime(net_env: "NetworkEnvBase") -> LabRuntime:
    require_backend_extra("kathara")
    try:
        from nika.runtime.kathara.runtime import KatharaRuntime
    except ImportError as exc:
        raise_missing_extra("kathara", cause=exc)
    return KatharaRuntime(net_env)


def runtime_for_session(meta: dict[str, Any] | Any) -> LabRuntime:
    """Build a runtime from persisted session metadata."""
    backend = resolve_backend(meta)
    lab_name = meta_lab_name(meta)
    if backend == "containerlab":
        topology_file = meta_path(meta, "topology_file", scenario_params=True)
        if topology_file is None:
            raise ValueError(f"Containerlab session {lab_name!r} has no topology_file.")
        return _containerlab_runtime(
            lab_name=lab_name,
            topology_file=topology_file,
            runtime_workdir=meta_path(meta, "runtime_workdir", scenario_params=True),
        )
    from nika.net_env.net_env_pool import get_net_env_instance

    scenario_name = meta_get(meta, "scenario_name")
    kwargs: dict[str, Any] = {"lab_name": lab_name, "backend": backend}
    scenario_params = dict(meta_get(meta, "scenario_params") or {})
    scenario_params.pop("backend", None)
    scenario_params.pop("topology_file", None)
    scenario_params.pop("runtime_workdir", None)
    if scenario_name:
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(str(scenario_name)):
            # Fixed ISP scale/topology are persisted as benchmark metadata; the
            # canonical scenario ID supplies both when reconstructing the lab.
            scenario_params.pop("topo_size", None)
            scenario_params.pop("topo", None)
    if scenario_params.get("topo_size") is not None:
        kwargs["topo_size"] = scenario_params.pop("topo_size")
    kwargs.update(scenario_params)
    if scenario_name:
        net_env = get_net_env_instance(str(scenario_name), **kwargs)
        return _kathara_runtime(net_env)
    raise ValueError("Kathara runtime requires scenario_name in session metadata.")


def runtime_for_net_env(net_env: "NetworkEnvBase") -> LabRuntime:
    """Build a runtime for a network environment instance."""
    if net_env.backend == "containerlab":
        if net_env.topology_file is None:
            raise ValueError(f"Containerlab env {net_env.name!r} has no topology_file.")
        return _containerlab_runtime(
            lab_name=net_env.name,
            topology_file=net_env.topology_file,
            runtime_workdir=net_env.runtime_workdir,
        )
    return _kathara_runtime(net_env)
