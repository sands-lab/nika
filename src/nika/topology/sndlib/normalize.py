"""Normalize SNDlib structures into a stable NetworkTopology IR."""

from __future__ import annotations

from collections.abc import Mapping

from nika.topology.errors import SndlibValidationError
from nika.topology.models import (
    NetworkTopology,
    TopoDemand,
    TopoLink,
    TopoNode,
)


def _unique_sorted_by_id(
    items: list[TopoNode] | list[TopoLink] | list[TopoDemand],
    *,
    kind: str,
    topology: str | None,
) -> tuple:
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in items:
        if item.id in seen:
            duplicates.append(item.id)
        else:
            seen.add(item.id)
    if duplicates:
        uniq = ", ".join(sorted(set(duplicates)))
        raise SndlibValidationError(
            f"duplicate {kind} id(s): {uniq}",
            topology=topology,
        )
    return tuple(sorted(items, key=lambda item: item.id))


def normalize_topology(
    *,
    name: str,
    meta: Mapping[str, str],
    nodes: list[TopoNode],
    links: list[TopoLink],
    demands: list[TopoDemand],
) -> NetworkTopology:
    """Sort entities by id and fail on duplicate ids."""
    sorted_nodes = _unique_sorted_by_id(nodes, kind="node", topology=name)
    sorted_links = _unique_sorted_by_id(links, kind="link", topology=name)

    normalized_demands: list[TopoDemand] = []
    for demand in demands:
        paths = tuple(sorted(demand.admissible_paths, key=lambda path: path.id))
        path_ids = [path.id for path in paths]
        if len(path_ids) != len(set(path_ids)):
            raise SndlibValidationError(
                f"duplicate admissible path id(s) in demand {demand.id!r}",
                topology=name,
            )
        normalized_demands.append(
            TopoDemand(
                id=demand.id,
                source=demand.source,
                target=demand.target,
                demand_value=demand.demand_value,
                routing_unit=demand.routing_unit,
                max_path_length=demand.max_path_length,
                admissible_paths=paths,
            )
        )
    sorted_demands = _unique_sorted_by_id(
        normalized_demands, kind="demand", topology=name
    )

    return NetworkTopology(
        name=name,
        source_format="sndlib-xml",
        meta=dict(meta),
        nodes=sorted_nodes,
        links=sorted_links,
        demands=sorted_demands,
    )
