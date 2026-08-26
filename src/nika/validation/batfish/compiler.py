from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network
from typing import Any, Literal

from nika.net_env.contract import NetworkEntity, ValidationIntent


@dataclass(frozen=True)
class BatfishQuestion:
    kind: Literal["reachability", "bgp_adjacency", "ospf_adjacency"]
    parameters: dict[str, Any]
    purpose: str


class UnsupportedIntentError(ValueError):
    pass


def compile_intent(intent: ValidationIntent) -> tuple[BatfishQuestion, ...]:
    if intent.property == "adjacency":
        assert intent.adjacency is not None
        kind = (
            "bgp_adjacency" if intent.adjacency.protocol == "bgp" else "ospf_adjacency"
        )
        return (
            BatfishQuestion(
                kind=kind,
                parameters=intent.adjacency.model_dump(mode="json"),
                purpose="find the expected modeled routing session",
            ),
        )

    if intent.source is None or intent.destination is None or intent.traffic is None:
        raise UnsupportedIntentError("flow intent has no concrete endpoints or traffic")
    base = {
        "path_constraints": {
            "startLocation": _location(intent.source),
            "endLocation": _end_location(intent.destination),
        },
        "headers": _headers(intent.source, intent.destination, intent),
        "max_traces": 5,
    }
    if intent.property == "reachability":
        return (
            BatfishQuestion(
                kind="reachability",
                parameters={**base, "actions": "failure"},
                purpose="find a flow that violates required reachability",
            ),
        )
    if intent.property == "isolation":
        return (
            BatfishQuestion(
                kind="reachability",
                parameters={**base, "actions": "success"},
                purpose="find a flow that violates required isolation",
            ),
        )
    if intent.property != "waypoint" or intent.path is None:
        raise UnsupportedIntentError(f"unsupported property {intent.property!r}")

    questions = [
        BatfishQuestion(
            kind="reachability",
            parameters={**base, "actions": "failure"},
            purpose="find an unreachable flow before checking its path",
        )
    ]
    if intent.path.must_avoid:
        path = dict(base["path_constraints"])
        path["transitLocations"] = ",".join(intent.path.must_avoid)
        questions.append(
            BatfishQuestion(
                kind="reachability",
                parameters={**base, "path_constraints": path, "actions": "success"},
                purpose="find a successful path through a forbidden node",
            )
        )
    for required_node in intent.path.must_traverse:
        path = dict(base["path_constraints"])
        path["forbiddenLocations"] = required_node
        questions.append(
            BatfishQuestion(
                kind="reachability",
                parameters={**base, "path_constraints": path, "actions": "success"},
                purpose=f"find a successful path avoiding required node {required_node}",
            )
        )
    return tuple(questions)


def _location(entity: NetworkEntity) -> str:
    if entity.kind in {"node", "endpoint"}:
        return entity.name
    if entity.node:
        return entity.node
    raise UnsupportedIntentError(f"source {entity.name!r} has no Batfish location")


def _end_location(entity: NetworkEntity) -> str | None:
    if entity.address is not None:
        return None
    if entity.kind in {"node", "endpoint"}:
        return entity.name
    if entity.kind == "service":
        return entity.node or entity.name
    return entity.node


def _headers(
    source: NetworkEntity, destination: NetworkEntity, intent: ValidationIntent
) -> dict[str, Any]:
    assert intent.traffic is not None
    headers: dict[str, Any] = {"dstIps": _destination_ip_space(destination)}
    if source.address:
        headers["srcIps"] = source.address
    protocol = intent.traffic.protocol
    if protocol != "ipv4":
        headers["ipProtocols"] = protocol.upper()
    if intent.traffic.destination_port is not None:
        headers["dstPorts"] = str(intent.traffic.destination_port)
    return headers


def _destination_ip_space(destination: NetworkEntity) -> str | None:
    if destination.kind != "prefix" or destination.address is None:
        return destination.address
    network = IPv4Network(destination.address)
    if network.num_addresses <= 2:
        return destination.address
    # Probe the same canonical address as the runtime verifier (first usable
    # host). Checking the whole usable range could pass statically while the
    # runtime probe address is filtered, producing a false mismatch.
    return str(network.network_address + 1)
