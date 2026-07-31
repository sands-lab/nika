"""Lab runtime abstraction for Kathara and Containerlab backends."""

from __future__ import annotations

from typing import Any

from nika.runtime.base import LabRuntime, RuntimeCapabilityError
from nika.runtime.factory import (
    resolve_backend,
    runtime_for_net_env,
    runtime_for_session,
)
from nika.runtime.spec import LabSpec, LinkSpec, NodeSpec

__all__ = [
    "ContainerlabRuntime",
    "KatharaRuntime",
    "LabRuntime",
    "LabSpec",
    "LinkSpec",
    "NodeSpec",
    "RuntimeCapabilityError",
    "parse_clab_topology",
    "render_topology",
    "resolve_backend",
    "runtime_for_net_env",
    "runtime_for_session",
]


def __getattr__(name: str) -> Any:
    if name == "KatharaRuntime":
        from nika.runtime.kathara import KatharaRuntime

        return KatharaRuntime
    if name == "ContainerlabRuntime":
        from nika.runtime.containerlab.runtime import ContainerlabRuntime

        return ContainerlabRuntime
    if name == "parse_clab_topology":
        from nika.runtime.containerlab.parse import parse_clab_topology

        return parse_clab_topology
    if name == "render_topology":
        from nika.runtime.containerlab.render import render_topology

        return render_topology
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
