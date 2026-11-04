"""OSPF adjacency and route state shared by failure recovery checks."""

import json

from nika.problems.support.router_ops import native_fault_ops


def full_frr_neighbors(raw: str) -> set[tuple[str, str]]:
    """Full adjacency identities, independent of transient timers and DR role."""
    neighbors = json.loads(raw).get("neighbors")
    if not isinstance(neighbors, dict):
        raise ValueError("Unreadable OSPF neighbors")
    return {
        (router_id, str(entry.get("ifaceName", "")))
        for router_id, entries in neighbors.items()
        for entry in (entries if isinstance(entries, list) else [entries])
        if isinstance(entry, dict) and str(entry.get("nbrState", "")).startswith("Full")
    }


def ospf_health(problem, host: str) -> dict[str, set]:
    """Read Full neighbors and installed OSPF routes for recovery checks."""
    from nika.validation.symptom.routing_probes import _ospf_routes

    if getattr(problem.net_env, "backend", None) == "containerlab":
        neighbors = problem.runtime.lab_api.srl_ospf_neighbors(host)
        return {
            "neighbors": {
                (n["router_id"], n["iface"])
                for n in neighbors
                if n["state"].lower().startswith("full")
            },
            "routes": _ospf_routes(problem, host),
        }
    ops = native_fault_ops(problem, host, "ospf_neighbors")
    neighbors = (
        {
            (neighbor.router_id, neighbor.interface)
            for neighbor in ops.ospf_neighbors()
            if neighbor.state.lower().startswith("full")
        }
        if ops is not None
        else full_frr_neighbors(
            problem.runtime.exec(host, "vtysh -c 'show ip ospf neighbor json'")
        )
    )
    return {"neighbors": neighbors, "routes": _ospf_routes(problem, host)}
