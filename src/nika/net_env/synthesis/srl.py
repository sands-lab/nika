"""Render exact-prefix BGP completion on SR Linux's existing YAML inputs."""

from __future__ import annotations

from copy import deepcopy
from ipaddress import IPv4Interface

from nika.net_env.synthesis.parameters import RoutingParameters


def bgp_sketch(documents, origins):
    """Select IPv4 eBGP as the experiment's control plane; retain addressing."""
    documents = deepcopy(documents)
    for node, document in documents.items():
        (instance,) = document["srl_nokia-network-instance:network-instance"]
        protocols = instance["protocols"]
        bgp = protocols["srl_nokia-bgp:bgp"]
        instance["protocols"] = {"srl_nokia-bgp:bgp": bgp}
        bgp["admin-state"] = "enable"
        bgp["afi-safi"] = [{"afi-safi-name": "ipv4-unicast", "admin-state": "enable"}]
        for neighbor in bgp["neighbor"]:
            neighbor["admin-state"] = "enable"
        local = [prefix for prefix, owner in origins.items() if owner == node]
        document["srl_nokia-routing-policy:routing-policy"] = {
            "prefix-set": [
                {
                    "name": "NC-ORIGIN",
                    "prefix": [
                        {
                            "ip-prefix": prefix,
                            "mask-length-range": f"{IPv4Interface(prefix).network.prefixlen}..{IPv4Interface(prefix).network.prefixlen}",
                        }
                        for prefix in local
                    ],
                }
            ]
            if local
            else [],
            "policy": [],
        }
    return documents


def apply_srl_exports(documents, parameters):
    """Bind solved per-neighbor export decisions without changing sessions."""
    solved = RoutingParameters.model_validate(parameters)
    if solved.ospf_costs or solved.isis_metrics or solved.import_preferences:
        raise ValueError("SR Linux completion currently supports BGP exports only")
    documents = deepcopy(documents)
    for index, policy in enumerate(solved.bgp_exports):
        if policy.node not in documents:
            raise ValueError("Unknown solved SR Linux node")
        document = documents[policy.node]
        (instance,) = document["srl_nokia-network-instance:network-instance"]
        bgp = instance["protocols"]["srl_nokia-bgp:bgp"]
        neighbors = [n for n in bgp["neighbor"] if n["peer-address"] == policy.neighbor]
        if len(neighbors) != 1:
            raise ValueError("Unknown or ambiguous solved SR Linux neighbor")
        routing = document["srl_nokia-routing-policy:routing-policy"]
        name = f"NC-OUT-{index}"
        statements = []
        for seq, term in enumerate(policy.terms, 1):
            prefix = IPv4Interface(term.prefix).network
            pset = f"{name}-{seq}"
            routing["prefix-set"].append(
                {
                    "name": pset,
                    "prefix": [
                        {
                            "ip-prefix": str(prefix),
                            "mask-length-range": f"{prefix.prefixlen}..{prefix.prefixlen}",
                        }
                    ],
                }
            )
            statements.append(
                {
                    "name": str(seq * 10),
                    "match": {"prefix-set": pset},
                    "action": {"policy-result": "accept" if term.permit else "reject"},
                }
            )
        statements.append({"name": "65535", "action": {"policy-result": "reject"}})
        routing["policy"].append({"name": name, "statement": statements})
        neighbors[0]["export-policy"] = [name]
        neighbors[0]["import-policy"] = ["NC-ACCEPT"]
        if not any(p["name"] == "NC-ACCEPT" for p in routing["policy"]):
            routing["policy"].append(
                {"name": "NC-ACCEPT", "default-action": {"policy-result": "accept"}}
            )
    expected = {
        (node, n["peer-address"])
        for node, document in documents.items()
        for n in document["srl_nokia-network-instance:network-instance"][0][
            "protocols"
        ]["srl_nokia-bgp:bgp"]["neighbor"]
    }
    if (
        solved.bgp_exports
        and {(p.node, p.neighbor) for p in solved.bgp_exports} != expected
    ):
        raise ValueError("SR Linux completion must cover every sketch neighbor")
    return documents
