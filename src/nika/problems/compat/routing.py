"""Routing-service identity retained by published benchmark releases."""

from pydantic import Field

from nika.problems.routing_control_plane.node_services import (
    RoutingServiceDown,
    RoutingServiceDownParams,
)


class FrrDownParams(RoutingServiceDownParams):
    service_name: str = Field(default="frr", description="Service name.")


class FrrDown(RoutingServiceDown):
    root_cause_name = "frr_service_down"
    description = "FRR routing daemon is unavailable."
    TAGS = ["frr"]
    Params = FrrDownParams


def published_fault_ontology(
    fault_types: list[str], scenario_name: str | None = None
) -> list[dict[str, str]]:
    """Retain the published descriptions independently of implementation aliases."""
    from nika.problems.ownership import ownership_entries

    entries = ownership_entries(fault_types, scenario_name)
    for entry in entries:
        if entry["id"] == FrrDown.root_cause_name:
            entry["description"] = FrrDown.description
    return entries
