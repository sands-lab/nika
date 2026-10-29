"""Healthy-baseline validation contract of the min3clos fabric design."""

from __future__ import annotations

from nika.net_env.contract import (
    AdjacencyExpectation,
    NetworkEntity,
    TrafficSelector,
    ValidationContract,
    ValidationIntent,
)
from nika.net_env.min3clos.verify import CLIENT1_GATEWAY, CLIENT2_GATEWAY

# Fabric links of configs/*.yaml: (leaf, leaf address, spine address).
_FABRIC = (("leaf1", "10.0.0.0", "10.0.0.1"), ("leaf2", "10.0.0.4", "10.0.0.5"))
_ASN = {"leaf1": 65001, "leaf2": 65002, "spine": 65056}
_ROUTER_ID = {"leaf1": "30.0.0.1", "leaf2": "30.0.0.2", "spine": "30.0.0.3"}
_GATEWAY = {"leaf1": CLIENT1_GATEWAY, "leaf2": CLIENT2_GATEWAY}


def build_min3clos_contract(scenario: str = "min3clos") -> ValidationContract:
    """Compile the fabric's eBGP sessions, OSPF adjacencies and client-subnet reachability."""
    intents = []
    for leaf, leaf_ip, spine_ip in _FABRIC:
        for local, remote, local_ip, remote_ip in (
            (leaf, "spine", leaf_ip, spine_ip),
            ("spine", leaf, spine_ip, leaf_ip),
        ):
            intents.append(
                ValidationIntent(
                    id=f"adj.bgp.{local}.{remote}",
                    description=f"eBGP session {local} to {remote} must be established.",
                    property="adjacency",
                    expected="established",
                    adjacency=AdjacencyExpectation(
                        protocol="bgp",
                        local_node=local,
                        remote_node=remote,
                        local_address=local_ip,
                        remote_address=remote_ip,
                        local_asn=_ASN[local],
                        remote_asn=_ASN[remote],
                        session_type="ebgp",
                    ),
                )
            )
        intents.append(
            ValidationIntent(
                id=f"adj.ospf.{leaf}.spine",
                description=f"OSPF adjacency {leaf} to spine must be Full.",
                property="adjacency",
                expected="established",
                adjacency=AdjacencyExpectation(
                    protocol="ospf",
                    local_node=leaf,
                    remote_node="spine",
                    local_address=leaf_ip,
                    remote_address=spine_ip,
                    ospf_area="0.0.0.0",
                    local_router_id=_ROUTER_ID[leaf],
                    remote_router_id=_ROUTER_ID["spine"],
                ),
            )
        )
    for source, destination in (("leaf1", "leaf2"), ("leaf2", "leaf1")):
        intents.append(
            ValidationIntent(
                id=f"reach.{source}.{destination}",
                description="Client gateways on different leaves must communicate.",
                property="reachability",
                expected="reachable",
                source=NetworkEntity(
                    kind="node", name=source, address=_GATEWAY[source]
                ),
                destination=NetworkEntity(
                    kind="node", name=destination, address=_GATEWAY[destination]
                ),
                traffic=TrafficSelector(protocol="icmp", round_trip=True),
            )
        )
    return ValidationContract(
        contract_id="min3clos.fabric",
        scenario=scenario,
        design_source={
            "basis": "Containerlab min-clos addressing, OSPFv2 area 0 and RFC 7938 eBGP sessions",
            "routers": sorted(_ASN),
        },
        intents=tuple(sorted(intents, key=lambda intent: intent.id)),
    )
