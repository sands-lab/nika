"""Render Nokia SR Linux BGP configuration blocks for ISP presets."""

from __future__ import annotations

from typing import Any

from nika.net_env.isp.bgp.plan import BgpNodePlan, BgpPlan


def render_bgp_srl_block(node: BgpNodePlan, plan: BgpPlan) -> dict[str, Any]:
    """Return an SRL ``srl_nokia-bgp:bgp`` dict for one router."""
    if plan.mode == "ibgp_rr":
        return _render_ibgp(node)
    if plan.mode == "ebgp":
        return _render_ebgp(node)
    return {}


def routing_policy_document() -> dict[str, Any]:
    """Shared BUSINESS prefix-set and BGP-IN/OUT policies.

    SRL has no BGP ``network`` statement; local/connected prefixes are advertised
    when the neighbor/group export-policy accepts them (prefix-set and/or
    protocol local). BGP-learned BUSINESS routes must also be accepted so RR
    reflection works.
    """
    prefixes = [
        "203.0.113.0/24",
        "203.0.114.0/24",
        "203.0.115.0/24",
        "198.51.100.0/24",
        "198.51.101.0/24",
        "198.51.102.0/24",
    ]
    return {
        "srl_nokia-routing-policy:routing-policy": {
            "prefix-set": [
                {
                    "name": "BUSINESS",
                    "prefix": [
                        {"ip-prefix": p, "mask-length-range": "24..24"}
                        for p in prefixes
                    ],
                }
            ],
            "policy": [
                {
                    "name": "BGP-OUT",
                    "statement": [
                        {
                            "name": "10",
                            "match": {"prefix-set": "BUSINESS"},
                            "action": {"policy-result": "accept"},
                        },
                        {
                            "name": "15",
                            "match": {
                                "prefix-set": "BUSINESS",
                                "protocol": "bgp",
                            },
                            "action": {"policy-result": "accept"},
                        },
                        {"name": "20", "action": {"policy-result": "reject"}},
                    ],
                },
                {
                    "name": "BGP-IN",
                    "statement": [
                        {
                            "name": "10",
                            "match": {"prefix-set": "BUSINESS"},
                            "action": {"policy-result": "accept"},
                        },
                        {"name": "20", "action": {"policy-result": "reject"}},
                    ],
                },
            ],
        }
    }


def _neighbors(
    node: BgpNodePlan, *, group: str, rr_clients: bool
) -> list[dict[str, Any]]:
    neighbors: list[dict[str, Any]] = []
    for sess in sorted(node.sessions, key=lambda s: (s.remote_ip, s.remote_device)):
        nb: dict[str, Any] = {
            "peer-address": sess.remote_ip,
            "peer-as": sess.remote_asn,
            "peer-group": group,
            "transport": {"local-address": sess.local_ip},
        }
        if rr_clients and sess.route_reflector_client:
            nb["route-reflector"] = {"client": True}
        neighbors.append(nb)
    return neighbors


def _render_ibgp(node: BgpNodePlan) -> dict[str, Any]:
    bgp: dict[str, Any] = {
        "admin-state": "enable",
        "autonomous-system": node.asn,
        "router-id": node.router_id,
        "afi-safi": [{"afi-safi-name": "ipv4-unicast", "admin-state": "enable"}],
        "group": [
            {
                "group-name": "IBGP",
                "admin-state": "enable",
                "peer-as": node.asn,
                "afi-safi": [
                    {
                        "afi-safi-name": "ipv4-unicast",
                        "admin-state": "enable",
                        "import-policy": ["BGP-IN"],
                        "export-policy": ["BGP-OUT"],
                    }
                ],
            }
        ],
        "neighbor": _neighbors(node, group="IBGP", rr_clients=True),
    }
    if node.cluster_id:
        bgp["route-reflector"] = {"cluster-id": node.cluster_id}
    return bgp


def _render_ebgp(node: BgpNodePlan) -> dict[str, Any]:
    return {
        "admin-state": "enable",
        "autonomous-system": node.asn,
        "router-id": node.router_id,
        # SRL rejects eBGP import/export unless a policy matches; disable the
        # default reject-all so group afi-safi BGP-IN/BGP-OUT policies apply.
        "ebgp-default-policy": {
            "import-reject-all": False,
            "export-reject-all": False,
        },
        "afi-safi": [{"afi-safi-name": "ipv4-unicast", "admin-state": "enable"}],
        "group": [
            {
                "group-name": "EBGP",
                "admin-state": "enable",
                "afi-safi": [
                    {
                        "afi-safi-name": "ipv4-unicast",
                        "admin-state": "enable",
                        "import-policy": ["BGP-IN"],
                        "export-policy": ["BGP-OUT"],
                    }
                ],
            }
        ],
        "neighbor": _neighbors(node, group="EBGP", rr_clients=True),
    }
