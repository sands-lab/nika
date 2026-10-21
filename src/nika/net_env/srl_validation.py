"""Evaluate existing adjacency and ICMP predicates on native SR Linux."""

from __future__ import annotations

from time import perf_counter
import shlex
import json
from ipaddress import IPv4Network

from nika.net_env.isp.fib import SRL_FIB_COMMAND, srl_fib
from nika.net_env.isp.igp.ifaces import srl_subinterface

from nika.net_env.contract import ValidationReport, ValidationResult
from nika.net_env.verify_collection import VerifyCommand, collect_verify_commands
from nika.service.pingmesh.parser import parse_ping_output

SRL_BGP_RIB_COMMAND = (
    'sr_cli "info from state network-instance default bgp-rib | as json"'
)


def selected_as_paths(data, prefix):
    """Read best/forwarding paths and their attributes from the SR Linux BGP RIB."""
    (instance,) = data["network-instance"]
    rib = instance["bgp-rib"]
    attributes = {
        str(a["index"]): a for a in rib.get("attr-sets", {}).get("attr-set", [])
    }
    paths = []
    for family in rib.get("afi-safi", []):
        if family["afi-safi-name"].split(":")[-1] != "ipv4-unicast":
            continue
        for route in (
            family.get("ipv4-unicast", {}).get("local-rib", {}).get("route", [])
        ):
            if route["prefix"] != prefix or not (
                route.get("best-route") or route.get("used-route")
            ):
                continue
            attr = attributes[str(route["attr-id"])]
            segments = sorted(
                attr.get("as-path", {}).get("segment", []),
                key=lambda s: s["as-path-index"],
            )
            if any(s["type"] != "as-sequence" for s in segments):
                raise ValueError("AS selection checks require sequence AS paths")
            paths.append(tuple(int(asn) for s in segments for asn in s["member"]))
    return paths


def verify_srl_contract(runtime, *, contract, plan=None):
    def request(intent):
        if intent.bgp_route:
            return VerifyCommand(intent.bgp_route.node, SRL_BGP_RIB_COMMAND, 30)
        if intent.route_next_hops:
            return VerifyCommand(intent.route_next_hops.node, SRL_FIB_COMMAND, 30)
        if intent.adjacency:
            protocol = intent.adjacency.protocol
            if protocol not in {"bgp", "isis", "ospf"}:
                raise NotImplementedError("Unsupported SR Linux protocol")
            return VerifyCommand(
                intent.adjacency.local_node,
                f'sr_cli "info from state network-instance default protocols {protocol} | as json"',
                30,
            )
        if (
            intent.property in {"reachability", "isolation"}
            and intent.traffic.protocol == "icmp"
        ):
            if (
                intent.source.kind != "node"
                or intent.destination.kind != "node"
                or not intent.source.address
                or not intent.destination.address
            ):
                raise NotImplementedError(
                    "SR Linux ICMP checks require addressed router nodes"
                )
            command = shlex.join(
                [
                    "ip",
                    "netns",
                    "exec",
                    "srbase-default",
                    "ping",
                    "-n",
                    "-c",
                    "3",
                    "-W",
                    "1",
                    "-I",
                    intent.source.address,
                    intent.destination.address,
                ]
            )
            return VerifyCommand(intent.source.name, command, 15)
        raise NotImplementedError("Unsupported SR Linux runtime predicate")

    requests, unsupported = {}, {}
    for intent in contract.intents:
        try:
            requests[intent.id] = request(intent)
        except NotImplementedError as exc:
            unsupported[intent.id] = exc
    collected = collect_verify_commands(runtime, list(requests.values()))
    rows = []
    for intent in contract.intents:
        started = perf_counter()
        evidence = {}
        try:
            if intent.id in unsupported:
                raise unsupported[intent.id]
            query = requests[intent.id]
            node, command = query.node, query.command
            result = collected.results[node, command]
            evidence = {
                "node": node,
                "command": command,
                "output": result.output,
                "returncode": result.returncode,
            }
            if result.error:
                raise ValueError(result.error)
            if intent.bgp_route:
                expected = intent.bgp_route
                paths = selected_as_paths(json.loads(result.output), expected.prefix)
                passed = bool(paths) == expected.present and all(
                    (expected.exit_asn is None or path[:1] == (expected.exit_asn,))
                    and (
                        expected.origin_asn is None
                        or path[-1:] == (expected.origin_asn,)
                    )
                    for path in paths
                )
                evidence.update(selected_as_paths=paths, expected=expected.model_dump())
            elif intent.route_next_hops:
                expected = intent.route_next_hops
                routes = srl_fib(json.loads(result.output))
                actual = {
                    h.gateway
                    for r in routes
                    if r.prefix == IPv4Network(expected.prefix) and not r.discard
                    for h in r.hops
                    if h.gateway
                }
                passed = bool(actual) and actual == set(expected.next_hops)
                evidence.update(
                    observed_next_hops=sorted(actual),
                    expected_next_hops=list(expected.next_hops),
                )
            elif intent.adjacency:
                adjacency = intent.adjacency
                state = json.loads(result.output)["network-instance"][0]["protocols"][
                    adjacency.protocol
                ]
                if adjacency.protocol == "bgp":
                    peer = next(
                        (
                            p
                            for p in state.get("neighbor", [])
                            if p["peer-address"] == adjacency.remote_address
                        ),
                        {},
                    )
                    up = peer.get("session-state") == "established"
                    passed = up == (intent.expected == "established")
                    if up:
                        passed = (
                            passed
                            and state["autonomous-system"] == adjacency.local_asn
                            and peer["peer-as"] == adjacency.remote_asn
                        )
                        if adjacency.route_reflector_client is not None:
                            passed = (
                                passed
                                and peer["route-reflector"]["client"]
                                == adjacency.route_reflector_client
                            )
                else:
                    if plan is None:
                        raise ValueError(
                            "IGP checks require the fixed original interface bindings"
                        )
                    node = next(
                        n for n in plan.nodes if n.device_name == adjacency.local_node
                    )
                    (interface,) = [
                        i
                        for i in node.interfaces
                        if i.address == adjacency.local_address
                    ]
                    instance = next(
                        i for i in state["instance"] if i["name"] == "default"
                    )
                    if adjacency.protocol == "isis":
                        row = next(
                            (
                                r
                                for r in instance["interface"]
                                if r["interface-name"]
                                == srl_subinterface(interface.name)
                            ),
                            {},
                        )
                        up = any(
                            p.get("state") == "up"
                            and p.get("neighbor-ipv4") == adjacency.remote_address
                            for p in row.get("adjacency", [])
                        )
                    else:
                        area = next(
                            a for a in instance["area"] if a["area-id"] == "0.0.0.0"
                        )
                        row = next(
                            (
                                r
                                for r in area["interface"]
                                if r["interface-name"]
                                == srl_subinterface(interface.name)
                            ),
                            {},
                        )
                        up = any(
                            p.get("adjacency-state", "").split(":")[-1] == "full"
                            and p.get("address") == adjacency.remote_address
                            and p.get("router-id") == adjacency.remote_router_id
                            for p in row.get("neighbor", [])
                        )
                    passed = up == (intent.expected == "established")
            elif (
                intent.property in {"reachability", "isolation"}
                and intent.traffic.protocol == "icmp"
            ):
                stats = parse_ping_output(result.output)
                if stats["status"] == "unknown":
                    raise ValueError("No valid ICMP probe result")
                passed = (stats["status"] == "ok") == (
                    intent.property == "reachability"
                )
            else:
                raise ValueError("Unsupported SR Linux runtime predicate")
            if (intent.adjacency or intent.bgp_route) and result.returncode:
                raise ValueError("SR Linux state query failed")
            status = "passed" if passed else "failed"
            reason = (
                None
                if passed
                else "Observed behavior differs from the original requirement"
            )
        except NotImplementedError as exc:
            status, reason = "unsupported", str(exc)
        except Exception as exc:
            status = "error"
            reason = str(exc)
        rows.append(
            ValidationResult(
                intent=intent.id,
                verifier="srl-runtime",
                status=status,
                supported=status != "unsupported",
                evidence=evidence,
                reason=reason,
                duration_ms=(perf_counter() - started) * 1000,
            )
        )
    return ValidationReport.from_results(contract, "srl-runtime", rows)
