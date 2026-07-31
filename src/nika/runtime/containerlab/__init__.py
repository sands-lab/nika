"""Containerlab backend runtime and topology helpers."""

from __future__ import annotations

from typing import Any

__all__ = ["ContainerlabRuntime", "parse_clab_topology", "render_topology"]


def __getattr__(name: str) -> Any:
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
