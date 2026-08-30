"""Integrity checks for normalized SNDlib topologies."""

from __future__ import annotations

from nika.topology.errors import SndlibValidationError
from nika.topology.models import CapacityModule, NetworkTopology, TopoLink


def _check_module(
    module: CapacityModule, *, context: str, topology: str | None
) -> None:
    if module.capacity < 0:
        raise SndlibValidationError(
            f"{context}: capacity must be >= 0, got {module.capacity}",
            topology=topology,
        )
    if module.cost < 0:
        raise SndlibValidationError(
            f"{context}: cost must be >= 0, got {module.cost}",
            topology=topology,
        )


def _check_link_costs(link: TopoLink, *, topology: str | None) -> None:
    if link.routing_cost is not None and link.routing_cost < 0:
        raise SndlibValidationError(
            f"link {link.id!r}: routingCost must be >= 0, got {link.routing_cost}",
            topology=topology,
        )
    if link.setup_cost is not None and link.setup_cost < 0:
        raise SndlibValidationError(
            f"link {link.id!r}: setupCost must be >= 0, got {link.setup_cost}",
            topology=topology,
        )
    if link.preinstalled is not None:
        _check_module(
            link.preinstalled,
            context=f"link {link.id!r} preInstalledModule",
            topology=topology,
        )
    for index, module in enumerate(link.additional_modules):
        _check_module(
            module,
            context=f"link {link.id!r} additionalModules[{index}]",
            topology=topology,
        )


def validate_topology(topology: NetworkTopology) -> None:
    """Raise SndlibValidationError if the topology is incomplete or inconsistent."""
    name = topology.name
    if not topology.nodes:
        raise SndlibValidationError("network has no nodes", topology=name)
    if not topology.links:
        raise SndlibValidationError("network has no links", topology=name)

    node_ids = {node.id for node in topology.nodes}
    link_ids = {link.id for link in topology.links}

    for link in topology.links:
        if link.source not in node_ids:
            raise SndlibValidationError(
                f"link {link.id!r}: unknown source node {link.source!r}",
                topology=name,
            )
        if link.target not in node_ids:
            raise SndlibValidationError(
                f"link {link.id!r}: unknown target node {link.target!r}",
                topology=name,
            )
        _check_link_costs(link, topology=name)

    for demand in topology.demands:
        if demand.source not in node_ids:
            raise SndlibValidationError(
                f"demand {demand.id!r}: unknown source node {demand.source!r}",
                topology=name,
            )
        if demand.target not in node_ids:
            raise SndlibValidationError(
                f"demand {demand.id!r}: unknown target node {demand.target!r}",
                topology=name,
            )
        if demand.demand_value <= 0:
            raise SndlibValidationError(
                f"demand {demand.id!r}: demandValue must be > 0, "
                f"got {demand.demand_value}",
                topology=name,
            )
        if demand.routing_unit is not None and demand.routing_unit < 1:
            raise SndlibValidationError(
                f"demand {demand.id!r}: routingUnit must be >= 1, "
                f"got {demand.routing_unit}",
                topology=name,
            )
        if demand.max_path_length is not None and demand.max_path_length < 1:
            raise SndlibValidationError(
                f"demand {demand.id!r}: maxPathLength must be >= 1, "
                f"got {demand.max_path_length}",
                topology=name,
            )
        for path in demand.admissible_paths:
            for link_id in path.link_ids:
                if link_id not in link_ids:
                    raise SndlibValidationError(
                        f"demand {demand.id!r} path {path.id!r}: "
                        f"unknown linkId {link_id!r}",
                        topology=name,
                    )
