"""Offline verification of observed ISP IPv4 forwarding, independent of backend."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from ipaddress import IPv4Address, IPv4Network
from typing import Any

from nika.net_env.isp.igp.ifaces import srl_ethernet_name
from nika.net_env.isp.igp.plan import (
    IspPlan,
    active_igp_links,
    igp_components,
    shortest_distances,
)

LINUX_FIB_COMMAND = "ip -j -4 route show table all"
SRL_FIB_COMMAND = (
    'sr_cli "info from state network-instance default route-table | as json"'
)


@dataclass(frozen=True)
class FibHop:
    interface: str
    gateway: str | None = None


@dataclass(frozen=True)
class FibRoute:
    prefix: IPv4Network
    hops: tuple[FibHop, ...] = ()
    local: bool = False
    discard: bool = False
    table: str = "main"
    priority: int = 0


def linux_fib(payload: Any) -> tuple[FibRoute, ...]:
    if not isinstance(payload, list):
        raise ValueError("Linux FIB is not a route list")
    routes = []
    for row in payload:
        if not isinstance(row, dict):
            raise ValueError("invalid Linux route")
        table = str(row.get("table", "main"))
        if table not in {"main", "local", "254", "255"}:
            continue
        prefix = IPv4Network("0.0.0.0/0" if row.get("dst") == "default" else row["dst"])
        kind = row.get("type", "unicast")
        if row.get("nhid") and not row.get("nexthops") and not row.get("dev"):
            raise ValueError(f"unresolved kernel nexthop id for {prefix}")
        hops = []
        for hop in row.get("nexthops") or [row]:
            if kind == "unicast":
                if not hop.get("dev") or set(hop.get("flags") or []) & {
                    "dead",
                    "linkdown",
                }:
                    raise ValueError(f"unusable next hop for {prefix}")
                hops.append(FibHop(hop["dev"], hop.get("gateway")))
        routes.append(
            FibRoute(
                prefix,
                tuple(hops),
                kind == "local",
                kind in {"blackhole", "unreachable", "prohibit", "throw"},
                table,
                int(row.get("metric", 0)),
            )
        )
    return tuple(routes)


def _unqualified(value: Any) -> Any:
    if isinstance(value, dict):
        return {key.split(":")[-1]: _unqualified(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unqualified(item) for item in value]
    return value


def srl_fib(payload: Any) -> tuple[FibRoute, ...]:
    """Resolve active, programmed NOS routes to their physical next hops."""
    payload = _unqualified(payload)
    if not isinstance(payload, dict):
        raise ValueError("SR Linux FIB is not an object")
    instances = payload.get("network-instance")
    if instances is not None:
        instances = instances if isinstance(instances, list) else [instances]
        instance = next(
            (row for row in instances if row.get("name") == "default"), None
        )
        if instance is None:
            raise ValueError("missing default network-instance")
        payload = instance
    table = payload.get("route-table", payload)
    if "ipv4-unicast" not in table:
        raise ValueError("missing IPv4 unicast route table")
    groups = {str(row["index"]): row for row in table.get("next-hop-group", [])}
    next_hops = {str(row["index"]): row for row in table.get("next-hop", [])}

    def programmed(row):
        state = row.get("fib-programming")
        if not isinstance(state, dict) or state.get(
            "last-successful-operation-type"
        ) not in {"add", "modify"}:
            raise ValueError("missing successful FIB programming state")
        if state.get("suppressed") or state.get("pending-operation-type") != "none":
            raise ValueError("FIB programming is suppressed or pending")
        if state.get("last-failed-operation-type", "none") != "none":
            try:
                failed = datetime.fromisoformat(
                    state["last-failed-operation-timestamp"]
                )
                succeeded = datetime.fromisoformat(
                    state["last-successful-operation-timestamp"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("invalid FIB programming timestamps") from exc
            if failed >= succeeded:
                raise ValueError("latest FIB programming operation failed")

    resolved_groups = {}

    def resolve(group_id, seen):
        key = str(group_id)
        if key in seen:
            raise ValueError("recursive next-hop group loop")
        if key in resolved_groups:
            return resolved_groups[key]
        group = groups.get(key)
        if group is None:
            raise ValueError(f"missing next-hop group {key}")
        programmed(group)
        hops = []
        local = discard = False
        for member in group.get("next-hop", []):
            if member.get("resolved") in {False, "false"}:
                continue
            hop = next_hops.get(str(member.get("next-hop", member.get("index"))))
            if hop is None:
                raise ValueError(f"missing next hop in group {key}")
            kind = hop.get("type")
            if kind in {"extract", "local", "receive"}:
                local = True
            elif kind in {"blackhole", "discard", "reject", "broadcast"}:
                discard = True
            elif kind == "indirect":
                route = hop.get("resolving-route") or {}
                children, child_local, child_discard = resolve(
                    route.get("next-hop-group"), seen | {key}
                )
                hops.extend(children)
                local |= child_local
                discard |= child_discard
            elif hop.get("subinterface"):
                hops.append(FibHop(hop["subinterface"], hop.get("ip-address")))
            else:
                raise ValueError(f"unresolved next hop in group {key}: {hop}")
        if not hops and not local and not discard:
            raise ValueError(f"empty next-hop group {key}")
        if (local or discard) and hops:
            raise ValueError(f"mixed forwarding and terminal actions in group {key}")
        resolved_groups[key] = tuple(hops), local, discard
        return resolved_groups[key]

    routes = []
    for row in table["ipv4-unicast"].get("route", []):
        if row.get("active") is not True:
            continue
        programmed(row)
        prefix = IPv4Network(row.get("ipv4-prefix", row.get("prefix")))
        hops, local, discard = resolve(row.get("next-hop-group"), set())
        routes.append(FibRoute(prefix, hops, local, discard))
    return tuple(routes)


class _RouteIndex:
    def __init__(self, routes: tuple[FibRoute, ...]) -> None:
        self.tables: dict[str, dict[tuple[int, int], FibRoute]] = {
            "local": {},
            "main": {},
        }
        for route in routes:
            table = "local" if route.table in {"local", "255"} else "main"
            key = route.prefix.prefixlen, int(route.prefix.network_address)
            previous = self.tables[table].get(key)
            if previous is None or route.priority < previous.priority:
                self.tables[table][key] = route

    def lookup(self, address: str) -> FibRoute | None:
        value = int(IPv4Address(address))
        # The default Linux RPDB consults local before main, then performs
        # longest-prefix matching and route-priority selection within a table.
        for table in self.tables.values():
            for length in range(32, -1, -1):
                mask = ((1 << 32) - 1) ^ ((1 << (32 - length)) - 1)
                route = table.get((length, value & mask))
                if route is not None:
                    return route
        return None


def verify_fib(
    plan: IspPlan,
    fibs: dict[str, tuple[FibRoute, ...]],
    *,
    errors: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Check all installed ECMP branches for every ordered intra-IGP pair."""
    errors = errors or {}
    graph = {node.device_name: [] for node in plan.nodes}
    links = {}
    for link in active_igp_links(plan):
        for node, peer, interface, gateway in (
            (link.endpoint_a, link.endpoint_b, link.iface_a, link.address_b),
            (link.endpoint_b, link.endpoint_a, link.iface_b, link.address_a),
        ):
            graph[node].append((peer, link.metric))
            for name in (interface, srl_ethernet_name(interface) + ".0"):
                links[(node, name, gateway)] = (peer, link.metric, link.link_id)
    indexes = {node: _RouteIndex(routes) for node, routes in fibs.items()}
    loopbacks = {node.device_name: node.loopback for node in plan.nodes}
    total = reachable = shortest = 0
    failures = []
    for component in igp_components(plan):
        for target in component:
            distances = shortest_distances(graph, target)
            memo: dict[str, tuple[bool, bool, int | None, int | None, dict]] = {}
            visiting = set()

            def walk(node):
                if node in memo:
                    return memo[node]
                evidence = {"failed_at": node}
                route = (
                    indexes[node].lookup(loopbacks[target]) if node in indexes else None
                )
                if node in visiting:
                    return (
                        False,
                        False,
                        None,
                        None,
                        evidence | {"reason": "forwarding_loop"},
                    )
                if node in errors or route is None:
                    result = (
                        False,
                        False,
                        None,
                        None,
                        evidence | {"reason": errors.get(node, "route_missing")},
                    )
                elif route.discard:
                    result = (
                        False,
                        False,
                        None,
                        None,
                        evidence | {"reason": "discard_route"},
                    )
                elif route.local:
                    result = (
                        node == target,
                        node == target,
                        0,
                        0,
                        {}
                        if node == target
                        else evidence | {"reason": "wrong_local_delivery"},
                    )
                else:
                    visiting.add(node)
                    paths = []
                    for hop in route.hops:
                        edge = links.get((node, hop.interface, hop.gateway))
                        detail = evidence | {
                            "interface": hop.interface,
                            "gateway": hop.gateway,
                        }
                        if edge is None:
                            paths.append(
                                (
                                    False,
                                    False,
                                    None,
                                    None,
                                    detail | {"reason": "unknown_igp_exit"},
                                )
                            )
                            continue
                        peer, cost, link_id = edge
                        ok, optimal, low, high, reason = walk(peer)
                        edge_optimal = distances.get(node) == cost + distances.get(
                            peer, 1 << 60
                        )
                        detail |= {"link_id": link_id, "reason": "non_shortest_exit"}
                        paths.append(
                            (
                                ok,
                                optimal and edge_optimal,
                                cost + low if low is not None else None,
                                cost + high if high is not None else None,
                                reason if reason else ({} if edge_optimal else detail),
                            )
                        )
                    visiting.remove(node)
                    lows = [path[2] for path in paths if path[2] is not None]
                    highs = [path[3] for path in paths if path[3] is not None]
                    result = (
                        bool(paths) and all(path[0] for path in paths),
                        bool(paths) and all(path[1] for path in paths),
                        min(lows) if lows else None,
                        max(highs) if highs else None,
                        next(
                            (path[4] for path in paths if path[4]),
                            evidence | {"reason": "empty_route"} if not paths else {},
                        ),
                    )
                memo[node] = result
                return result

            for source in component:
                if source == target:
                    continue
                total += 1
                ok, optimal, low, high, evidence = walk(source)
                reachable += ok
                shortest += ok and optimal
                if (not ok or not optimal) and len(failures) < 20:
                    failures.append(
                        {
                            "source": source,
                            "target": target,
                            "destination": loopbacks[target],
                            "actual_cost": [low, high],
                            "expected_cost": distances.get(source),
                            **evidence,
                        }
                    )
    return {
        "all_pairs_reachable": reachable == total and not errors,
        "shortest_paths": shortest == total and not errors,
        "pairs_checked": total,
        "reachable_pairs": reachable,
        "shortest_pairs": shortest,
        "failure_count": total - shortest,
        "failures": failures,
        "collection_errors": errors,
    }
