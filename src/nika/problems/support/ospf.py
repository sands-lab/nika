"""OSPF adjacency and route state shared by failure recovery checks."""

import json
import re

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

    ops = native_fault_ops(problem, host, "ospf_neighbors")
    if getattr(problem.net_env, "backend", None) == "containerlab" and ops is None:
        neighbors = problem.runtime.lab_api.srl_ospf_neighbors(host)
        return {
            "neighbors": {
                (n["router_id"], n["iface"])
                for n in neighbors
                if n["state"].lower().startswith("full")
            },
            "routes": _ospf_routes(problem, host),
        }
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


_ROUTER_LSA_SECTION_RE = re.compile(r"Router Link States \(Area ([\d.]+)")
_LSA_ROW_RE = re.compile(r"^(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)\s+\d+\s+0x")


def router_lsa_origins(database: str) -> dict[str, set[str]]:
    """Area -> advertising routers of its router-LSAs, from ``show ip ospf database``."""
    origins: dict[str, set[str]] = {}
    area = None
    for line in database.splitlines():
        if match := _ROUTER_LSA_SECTION_RE.search(line):
            area = match[1]
            origins.setdefault(area, set())
        elif "Link States" in line:
            area = None
        elif area and (match := _LSA_ROW_RE.match(line.strip())):
            origins[area].add(match[2])
    return origins
