"""Compare normalized routing facts of two concrete candidates for one design.

The comparator is vendor-neutral: each candidate is first exported through the
snapshot builder (native parser or documented restricted projection) that the
scenario validation workflow already uses for that platform. Batfish then
derives the same fact families from both snapshots. Interface names are mapped
back to the topology's logical names through each snapshot's
``interface_aliases`` metadata, so facts compare across syntaxes.

Fact families:

* ``interface``: routed interface addresses, VRF and activity.
* ``igp_interface``: OSPF area, passivity and cost per enabled interface.
* ``igp_adjacency``: OSPF sessions or IS-IS edges with their remote endpoint.
* ``bgp_session``: configured peers, ASNs, RR-client role, cluster and status.
* ``bgp_policy``: per-neighbor import/export outcome for identical probe routes.
* ``bgp_route``: selected BGP paths (AS path, local preference, next hop).
* ``rib``: main-RIB protocol, next hops, metric and administrative distance.
* ``fib``: recursively resolved egress interface and gateway set.
"""

from __future__ import annotations

import json
from collections import defaultdict
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

FACT_KINDS = (
    "interface",
    "igp_interface",
    "igp_adjacency",
    "bgp_session",
    "bgp_policy",
    "bgp_route",
    "rib",
    "fib",
)

# RFC 5737 TEST-NET-1: outside every NIKA business and infrastructure allow-list.
CANARY_PREFIX = "192.0.2.0/24"

_NO_IP = {None, "", "AUTO/NONE(-1l)", "None"}
_NO_INTERFACE = {None, "", "dynamic", "None"}
_NULL_INTERFACES = {"null_interface", "Null0", "null0"}


class FactDifference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    node: str
    key: str
    native: Any = None
    candidate: Any = None


class ConsistencyReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: str
    deployment: dict[str, Any]
    candidate: str
    routing_model: str
    scope: dict[str, Any]
    fact_counts: dict[str, dict[str, int]]
    differences: tuple[FactDifference, ...]

    @property
    def consistent(self) -> bool:
        return not self.differences

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for difference in self.differences:
            counts[difference.kind] += 1
        return dict(sorted(counts.items()))

    def write(self, path: Path) -> None:
        payload = self.model_dump(mode="json")
        payload["status"] = "consistent" if self.consistent else "different"
        payload["difference_summary"] = self.summary()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


Facts = dict[tuple[str, str, str], Any]


def _split_interface(value: Any) -> tuple[str, str]:
    text = str(value)
    if "[" not in text or not text.endswith("]"):
        raise ValueError(f"Unexpected Batfish interface reference {text!r}")
    node, interface = text[:-1].split("[", 1)
    return node, interface


def _alias(aliases: dict[str, dict[str, str]], node: str, interface: Any) -> Any:
    if interface in _NO_INTERFACE:
        return None
    return aliases.get(node, {}).get(str(interface), str(interface))


def _ip(value: Any) -> str | None:
    return None if value in _NO_IP else str(value)


def _sorted_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return sorted(str(item) for item in value)
    return [str(value)]


def interface_facts(rows, aliases) -> Facts:
    facts: Facts = {}
    for row in rows:
        node, interface = _split_interface(row["Interface"])
        prefixes = _sorted_list(row.get("All_Prefixes"))
        if not prefixes:
            continue
        facts["interface", node, _alias(aliases, node, interface)] = {
            "active": bool(row.get("Active")),
            "prefixes": prefixes,
            "vrf": row.get("VRF") or "default",
        }
    return facts


def ospf_interface_facts(rows, aliases) -> Facts:
    facts: Facts = {}
    for row in rows:
        if not row.get("OSPF_Enabled"):
            continue
        node, interface = _split_interface(row["Interface"])
        facts["igp_interface", node, _alias(aliases, node, interface)] = {
            "protocol": "ospf",
            "area": str(row.get("OSPF_Area_Name")),
            "passive": bool(row.get("OSPF_Passive")),
            "cost": row.get("OSPF_Cost"),
        }
    return facts


def _remote(aliases, value) -> str:
    node, interface = _split_interface(value)
    return f"{node}[{_alias(aliases, node, interface)}]"


def ospf_adjacency_facts(rows, aliases) -> Facts:
    facts: Facts = {}
    for row in rows:
        node, interface = _split_interface(row["Interface"])
        key = f"ospf {_alias(aliases, node, interface)}"
        facts["igp_adjacency", node, key] = {
            "remote": _remote(aliases, row["Remote_Interface"]),
            "status": row.get("Session_Status"),
        }
    return facts


def isis_adjacency_facts(rows, aliases) -> Facts:
    facts: Facts = {}
    for row in rows:
        node, interface = _split_interface(row["Interface"])
        key = f"isis {_alias(aliases, node, interface)}"
        facts["igp_adjacency", node, key] = {
            "remote": _remote(aliases, row["Remote_Interface"]),
            "status": "ESTABLISHED",
        }
    return facts


def bgp_session_facts(configured, status) -> Facts:
    states = {
        (row["Node"], row.get("VRF") or "default", _ip(row.get("Remote_IP"))): row
        for row in status
    }
    facts: Facts = {}
    for row in configured:
        node, vrf = row["Node"], row.get("VRF") or "default"
        remote = _ip(row.get("Remote_IP"))
        state = states.get((node, vrf, remote), {})
        facts["bgp_session", node, f"{vrf} {remote}"] = {
            "local_as": row.get("Local_AS"),
            "remote_as": str(row.get("Remote_AS")),
            "local_ip": _ip(row.get("Local_IP")),
            "rr_client": bool(row.get("Route_Reflector_Client")),
            "cluster_id": _ip(row.get("Cluster_ID")),
            "session_type": state.get("Session_Type"),
            "remote_node": state.get("Remote_Node"),
            "status": state.get("Established_Status", "NOT_COMPATIBLE"),
        }
    return facts


def _as_path(value: Any) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple)):
        # pybatfish route objects carry AS_SEQUENCE segments as {"asns": [...]}.
        return tuple(
            int(asn)
            for item in value
            for asn in (item.get("asns", []) if isinstance(item, dict) else [item])
        )
    return tuple(int(token) for token in str(value).replace("{", "").split() if token)


def _route_entry(row: dict[str, Any], aliases) -> dict[str, Any]:
    return {
        "protocol": row.get("Protocol"),
        "next_hop_ip": _ip(row.get("Next_Hop_IP")),
        "next_hop_interface": _alias(
            aliases, row["Node"], row.get("Next_Hop_Interface")
        ),
        "metric": row.get("Metric"),
        "admin_distance": row.get("Admin_Distance"),
    }


def rib_facts(rows, aliases) -> Facts:
    grouped: dict[tuple[str, str, str], list] = defaultdict(list)
    for row in rows:
        grouped[
            "rib", row["Node"], f"{row.get('VRF') or 'default'} {row['Network']}"
        ].append(_route_entry(row, aliases))
    return {
        key: sorted(entries, key=lambda e: json.dumps(e, sort_keys=True))
        for key, entries in grouped.items()
    }


def bgp_route_facts(rows) -> Facts:
    grouped: dict[tuple[str, str, str], list] = defaultdict(list)
    for row in rows:
        status = _sorted_list(row.get("Status"))
        if not {"BEST", "MULTIPATH"} & set(status):
            continue
        grouped[
            "bgp_route", row["Node"], f"{row.get('VRF') or 'default'} {row['Network']}"
        ].append(
            {
                "as_path": list(_as_path(row.get("AS_Path"))),
                "local_pref": row.get("Local_Pref"),
                "next_hop_ip": _ip(row.get("Next_Hop_IP")),
                "communities": _sorted_list(row.get("Communities")),
                "origin_type": row.get("Origin_Type"),
            }
        )
    return {
        key: sorted(entries, key=lambda e: json.dumps(e, sort_keys=True))
        for key, entries in grouped.items()
    }


def resolve_fib(rows, aliases, *, max_depth: int = 8) -> Facts:
    """Resolve each main-RIB prefix to its egress interfaces and gateways.

    Recursive next hops use longest-prefix match in the same node and VRF, as a
    FIB does. Discard routes resolve to ``null``; unresolvable ones to nothing.
    """
    table: dict[tuple[str, str], list[tuple[IPv4Network, dict]]] = defaultdict(list)
    for row in rows:
        vrf = row.get("VRF") or "default"
        table[row["Node"], vrf].append((IPv4Network(row["Network"]), row))
    for entries in table.values():
        entries.sort(key=lambda item: -item[0].prefixlen)

    def lookup(node, vrf, address):
        target = IPv4Address(address)
        best = None
        for network, row in table[node, vrf]:
            if target in network:
                if best is None:
                    best = network
                if network != best:
                    break
                yield row

    def resolve(node, vrf, row, depth):
        interface = row.get("Next_Hop_Interface")
        gateway = _ip(row.get("Next_Hop_IP"))
        if interface in _NULL_INTERFACES or str(row.get("Next_Hop", "")).startswith(
            "discard"
        ):
            return {("null", None)}
        if interface not in _NO_INTERFACE:
            return {(_alias(aliases, node, interface), gateway)}
        if gateway is None or depth >= max_depth:
            return set()
        result = set()
        for next_row in lookup(node, vrf, gateway):
            if next_row is row:
                continue
            for egress, next_gateway in resolve(node, vrf, next_row, depth + 1):
                result.add((egress, next_gateway or gateway))
        return result

    facts: Facts = {}
    for (node, vrf), entries in table.items():
        by_prefix: dict[str, set] = defaultdict(set)
        for network, row in entries:
            by_prefix[str(network)] |= resolve(node, vrf, row, 0)
        for prefix, hops in by_prefix.items():
            facts["fib", node, f"{vrf} {prefix}"] = sorted(
                [list(hop) for hop in hops], key=lambda hop: (hop[0], hop[1] or "")
            )
    return facts


def probe_routes(
    bgp_rows_by_side, declared_prefixes, communities=()
) -> tuple[tuple[str, tuple[int, ...], tuple[str, ...]], ...]:
    """Build identical BGP probe inputs from both candidates and the design.

    Probes include every advertised path of either candidate, each declared
    prefix without attributes and, when the design declares signaling
    communities, the same prefix tagged with each community.
    """
    probes = set()
    for rows in bgp_rows_by_side:
        for row in rows:
            probes.add(
                (
                    str(row["Network"]),
                    _as_path(row.get("AS_Path")),
                    tuple(_sorted_list(row.get("Communities"))),
                )
            )
    for prefix in {*declared_prefixes, CANARY_PREFIX}:
        probes.add((str(IPv4Network(prefix)), (), ()))
        for community in communities:
            probes.add((str(IPv4Network(prefix)), (), (str(community),)))
    return tuple(sorted(probes))


def probe_id(probe) -> str:
    prefix, as_path, communities = probe
    return (
        f"{prefix} path=[{' '.join(map(str, as_path))}]"
        f" communities=[{' '.join(communities)}]"
    )


def policy_outcome(row: dict[str, Any] | None, input_route) -> dict[str, Any]:
    """Reduce one testRoutePolicies row to its routing-relevant result.

    ``row=None`` means the peer has no policy in that direction, so the route
    passes unchanged. Next hops are reported only when a policy rewrites them.
    """
    source = _route_dict(input_route)
    if row is None:
        action, output = "PERMIT", source
    else:
        action = str(row.get("Action", "")).upper()
        output = _route_dict(row.get("Output_Route"))
    if action != "PERMIT":
        return {"action": action}
    next_hop = output.get("nextHopIp")
    return {
        "action": action,
        "as_path": list(_as_path(output.get("asPath"))),
        "communities": _sorted_list(output.get("communities")),
        "local_pref": output.get("localPreference"),
        "metric": output.get("metric"),
        "next_hop": "unchanged"
        if next_hop in (None, source.get("nextHopIp"))
        else str(next_hop),
        "weight": output.get("weight"),
    }


def diff_facts(native: Facts, candidate: Facts) -> tuple[FactDifference, ...]:
    differences = []
    for key in sorted(set(native) | set(candidate)):
        left, right = native.get(key), candidate.get(key)
        if left != right:
            kind, node, detail = key
            differences.append(
                FactDifference(
                    kind=kind, node=node, key=detail, native=left, candidate=right
                )
            )
    return tuple(differences)


def fact_counts(facts: Facts) -> dict[str, int]:
    counts = {kind: 0 for kind in FACT_KINDS}
    for kind, _, _ in facts:
        counts[kind] += 1
    return counts


class BatfishFactCollector:
    """Derive routing facts from one Batfish snapshot through pybatfish."""

    def __init__(self, *, host: str = "127.0.0.1", port: int = 9996) -> None:
        from nika.validation.batfish.client import BatfishClient

        self._client = BatfishClient(host=host, port=port)

    @property
    def _session(self):
        return self._client._session

    def load(self, snapshot) -> dict[str, Any]:
        info = self._client.initialize(snapshot)
        self._aliases = snapshot.metadata.get("interface_aliases", {})
        self._snapshot = info["snapshot"]
        status = self._frame(self._session.q.fileParseStatus())
        bad = [
            row
            for row in status
            if str(row.get("Status", "")).upper()
            not in {"PASSED", "PARTIALLY_UNRECOGNIZED"}
        ]
        if bad:
            raise RuntimeError(f"Batfish could not parse candidate files: {bad}")
        info["parse_status"] = {
            row["File_Name"]: row["Status"] for row in status if row.get("File_Name")
        }
        return info

    def _frame(self, question) -> list[dict[str, Any]]:
        from nika.validation.batfish.client import _records

        return _records(question.answer(self._snapshot).frame())

    def bgp_rib(self) -> list[dict[str, Any]]:
        return self._frame(self._session.q.bgpRib())

    def collect(self, probes) -> Facts:
        q = self._session.q
        aliases = self._aliases
        facts: Facts = {}
        facts.update(
            interface_facts(
                self._frame(
                    q.interfaceProperties(properties="Active,All_Prefixes,VRF")
                ),
                aliases,
            )
        )
        facts.update(
            ospf_interface_facts(self._frame(q.ospfInterfaceConfiguration()), aliases)
        )
        facts.update(
            ospf_adjacency_facts(self._frame(q.ospfSessionCompatibility()), aliases)
        )
        facts.update(
            isis_adjacency_facts(self._frame(q.edges(edgeType="isis")), aliases)
        )
        configured = self._frame(q.bgpPeerConfiguration())
        facts.update(bgp_session_facts(configured, self._frame(q.bgpSessionStatus())))
        facts.update(bgp_route_facts(self.bgp_rib()))
        routes = self._frame(q.routes())
        facts.update(rib_facts(routes, aliases))
        facts.update(resolve_fib(routes, aliases))
        facts.update(self._policy_facts(configured, probes))
        return facts

    def _policy_facts(self, configured, probes) -> Facts:
        from pybatfish.datamodel.route import BgpRoute

        facts: Facts = {}
        for row in configured:
            node, remote = row["Node"], _ip(row.get("Remote_IP"))
            vrf = row.get("VRF") or "default"
            local_as = row.get("Local_AS")
            try:
                remote_as = int(str(row.get("Remote_AS")))
            except ValueError:
                remote_as = None
            external = remote_as is not None and remote_as != local_as
            for direction, column in (
                ("in", "Import_Policy"),
                ("out", "Export_Policy"),
            ):
                policies = _sorted_list(row.get(column))
                if len(policies) > 1:
                    raise ValueError(
                        f"{node} {remote}: expected one {direction} policy, got {policies}"
                    )
                inputs = []
                for probe in probes:
                    prefix, as_path, communities = probe
                    if direction == "in" and external:
                        as_path = (remote_as, *as_path)
                    inputs.append(
                        (
                            probe,
                            BgpRoute(
                                network=prefix,
                                originatorIp="0.0.0.0",
                                originType="igp",
                                protocol="bgp",
                                asPath=list(as_path),
                                communities=list(communities),
                                localPreference=100,
                                nextHopIp=remote if direction == "in" else "0.0.0.1",
                                sourceProtocol="bgp",
                            ),
                        )
                    )
                results: dict[str, dict] = {}
                if policies:
                    # Keep pybatfish route objects; the record helper stringifies them.
                    answer = (
                        self._session.q.testRoutePolicies(
                            nodes=json.dumps(node),
                            policies=json.dumps(policies[0]),
                            direction=direction,
                            inputRoutes=[route for _, route in inputs],
                        )
                        .answer(self._snapshot)
                        .frame()
                        .to_dict(orient="records")
                    )
                    for result in answer:
                        results[_input_key(result.get("Input_Route"))] = result
                for probe, route in inputs:
                    row_result = (
                        results.get(_input_key(route.dict())) if policies else None
                    )
                    if policies and row_result is None:
                        raise RuntimeError(
                            f"Missing route-policy result for {node} {remote} {probe}"
                        )
                    facts[
                        "bgp_policy",
                        node,
                        f"{vrf} {remote} {direction} {probe_id(probe)}",
                    ] = policy_outcome(row_result, route)
        return facts


def _route_dict(route) -> dict[str, Any]:
    if route is None:
        return {}
    if isinstance(route, str):
        return json.loads(route)
    return route if isinstance(route, dict) else route.dict()


def _input_key(route) -> str:
    route = _route_dict(route)
    return json.dumps(
        [
            route.get("network"),
            list(_as_path(route.get("asPath"))),
            sorted(str(c) for c in route.get("communities") or []),
        ]
    )
