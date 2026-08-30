"""Backend-neutral network topology IR (SNDlib import and future lab builders)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class TopoNode:
    id: str
    x: float | None = None
    y: float | None = None


@dataclass(frozen=True)
class CapacityModule:
    capacity: float
    cost: float


@dataclass(frozen=True)
class TopoLink:
    id: str
    source: str
    target: str
    routing_cost: float | None = None
    setup_cost: float | None = None
    preinstalled: CapacityModule | None = None
    additional_modules: tuple[CapacityModule, ...] = ()


@dataclass(frozen=True)
class AdmissiblePath:
    id: str
    link_ids: tuple[str, ...]


@dataclass(frozen=True)
class TopoDemand:
    id: str
    source: str
    target: str
    demand_value: float
    routing_unit: int | None = None
    max_path_length: int | None = None
    admissible_paths: tuple[AdmissiblePath, ...] = ()


@dataclass(frozen=True)
class NetworkTopology:
    name: str
    source_format: Literal["sndlib-xml"]
    meta: Mapping[str, str]
    nodes: tuple[TopoNode, ...]
    links: tuple[TopoLink, ...]
    demands: tuple[TopoDemand, ...]


def link_preinstalled_capacity(link: TopoLink) -> float | None:
    """Return preinstalled capacity only; never synthesize from additional modules."""
    if link.preinstalled is None:
        return None
    return link.preinstalled.capacity
