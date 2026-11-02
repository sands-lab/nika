"""Observe area reachability and summary LSAs across an OSPF ABR."""

from __future__ import annotations

import ipaddress
import json
import re

from nika.net_env.native_routers import router_sketches
from nika.net_env.verify import ping_stats
from nika.problems.base import build_verify_result
from nika.problems.routing_control_plane.ospf import hosts_behind_router, ospf_networks
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops
from nika.validation.symptom.routing_probes import (
    _connected_routes,
    _ospf_intf_neighbor_full,
    _ospf_routes,
)


def _probe_ends(problem, branch, abr):
    sketches = router_sketches(problem.net_env)
    backbone = {
        ipaddress.IPv4Network(prefix)
        for config, _ in sketches.values()
        for prefix, area in ospf_networks(config)
        if area in {"0", "0.0.0.0"}
    }
    src = None
    for name in sorted(problem.net_env.lab.machines):
        if name in sketches:
            continue
        path = f"{name}.startup"
        if not problem.net_env.lab.fs.exists(path):
            continue
        startup = problem.net_env.lab.fs.readtext(path)
        if any(
            ipaddress.IPv4Interface(address).ip in network
            for address in re.findall(r"ip addr add ([0-9./]+) dev", startup)
            for network in backbone
        ):
            src = name
            break
    # The distribution gateway answers on an access subnet on each router OS.
    destinations = [
        ipaddress.IPv4Interface(address)
        for host in branch["members"]
        for address in re.findall(r"ip addr add ([0-9./]+) dev", sketches[host][1])
        if str(ipaddress.IPv4Interface(address).network) in branch["prefixes"]
        and ipaddress.IPv4Interface(address).network.prefixlen < 31
    ]
    if not src or not destinations:
        raise ValueError(
            "No backbone host and non-backbone access gateway for OSPF range probe"
        )
    gateway = min(destinations, key=lambda a: int(a.ip))
    distribution = next(
        host
        for host in branch["members"]
        if str(gateway) in re.findall(r"ip addr add ([0-9./]+) dev", sketches[host][1])
    )
    inside = hosts_behind_router(problem.net_env, distribution)
    abr_addresses = [
        ipaddress.IPv4Interface(address)
        for address in re.findall(r"ip addr add ([0-9./]+) dev", sketches[abr][1])
        if str(ipaddress.IPv4Interface(address).network) in branch["prefixes"]
    ]
    if not inside or not abr_addresses:
        raise ValueError("No intra-area host and ABR address for OSPF range probe")
    return {
        "src": src,
        "dst": str(gateway.ip),
        "control_src": inside[0],
        "control_dst": str(min(abr_addresses, key=lambda a: int(a.ip)).ip),
    }


def _summary_ids(problem, host):
    ops = native_fault_ops(problem, host, "ospf_summary_lsa_ids")
    if ops is not None:
        return ops.ospf_summary_lsa_ids("0.0.0.0")
    output = problem.runtime.exec(host, "vtysh -c 'show ip ospf database summary json'")
    return frr_summary_lsa_ids(output, "0.0.0.0")


def frr_summary_lsa_ids(output: str, area: str) -> set[str]:
    """Live Type-3 LSA IDs from FRR's area-scoped detailed JSON."""
    data = json.loads(output)
    rows = data["summaryLinkStates"]["areas"].get(area, [])
    return {row["linkStateId"] for row in rows if row["lsaAge"] < 3600}


def _snapshot(problem, branch, ends):
    prefixes = set(branch["prefixes"])
    routers = sorted(branch["interfaces"])
    routes = {
        host: sorted(
            prefixes & (_ospf_routes(problem, host) | _connected_routes(problem, host))
        )
        for host in routers
    }
    full = {
        f"{host}:{intf}": _ospf_intf_neighbor_full(problem, host, intf)
        for host in routers
        for intf in branch["interfaces"][host]
    }
    return {
        "routes": routes,
        "full": full,
        "summaries": sorted(_summary_ids(problem, branch["backbone_observer"])),
        "intra_area_loss_percent": ping_stats(
            problem.runtime,
            ends["control_src"],
            ends["control_dst"],
            count=3,
            interval_sec=0.2,
        ).loss_percent,
        "loss_percent": ping_stats(
            problem.runtime, ends["src"], ends["dst"], count=5, interval_sec=0.2
        ).loss_percent,
    }


def _healthy(branch, snapshot):
    prefixes = set(branch["prefixes"])
    ids = {str(ipaddress.IPv4Network(p).network_address) for p in prefixes}
    return (
        all(set(routes) == prefixes for routes in snapshot["routes"].values())
        and bool(snapshot["full"])
        and all(value is True for value in snapshot["full"].values())
        and ids <= set(snapshot["summaries"])
        and snapshot["loss_percent"] == 0
        and snapshot["intra_area_loss_percent"] == 0
    )


def area_range_baseline(problem, params):
    branch = problem.branch(params)
    ends = _probe_ends(problem, branch, params.host_name)
    latest = {}

    def ready():
        latest.update(_snapshot(problem, branch, ends))
        return _healthy(branch, latest)

    ok = wait_until(ready, 180, interval_s=3)
    if ok:
        problem._audit_ospf_range = {
            "host": params.host_name,
            "branch": branch,
            "ends": ends,
            "before": dict(latest),
        }
    return ok, {**ends, "branch": branch, "before": latest}


def area_range_suppressed(problem, params):
    base = getattr(problem, "_audit_ospf_range", None)
    if not base or base["host"] != params.host_name:
        return False, {"error": "no_healthy_area_range_baseline"}
    branch = base["branch"]
    prefixes = set(branch["prefixes"])
    ids = {str(ipaddress.IPv4Network(p).network_address) for p in prefixes}
    latest = {}

    def suppressed():
        latest.update(_snapshot(problem, branch, base["ends"]))
        return (
            all(set(latest["routes"][host]) == prefixes for host in branch["members"])
            and all(not latest["routes"][host] for host in branch["observers"])
            and latest["full"] == base["before"]["full"]
            and not ids.intersection(latest["summaries"])
            and set(base["before"]["summaries"]) - ids <= set(latest["summaries"])
            and latest["loss_percent"] == 100
            and latest["intra_area_loss_percent"] == 0
        )

    ok = wait_until(suppressed, 180, interval_s=3)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={**base, "after": latest},
    )


def area_range_restored(problem, params):
    base = getattr(problem, "_audit_ospf_range", None)
    if not base or base["host"] != params.host_name:
        return False, {"error": "no_healthy_area_range_baseline"}
    latest = {}

    def restored():
        latest.update(_snapshot(problem, base["branch"], base["ends"]))
        return _healthy(base["branch"], latest)

    ok = wait_until(restored, 180, interval_s=3)
    return ok, {**base["ends"], "restored": latest}
