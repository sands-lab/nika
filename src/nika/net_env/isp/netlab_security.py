"""Add FRR-only security policy to netlab-generated eBGP configurations."""

from __future__ import annotations

import re

from nika.net_env.isp.bgp.frr import (
    EXPORT_DENY_PREFIX_LIST,
    _leak_prefix_list,
    _leaker_outbound_route_maps,
    _rpki_reject_route_map,
    _rpki_stanza,
    _rtbh_discard_static,
    _rtbh_import_route_map,
)
from nika.net_env.isp.bgp.plan import BgpPlan


_BINDING = re.compile(
    r"^\s*neighbor ([0-9.]+) route-map ([A-Za-z0-9_.-]+) (in|out)$", re.M
)


def apply_netlab_security_overlay(
    configs: dict[str, str], transformed: dict, plan: BgpPlan
) -> dict[str, str]:
    """Append the plan's RPKI/RTBH policy to actual netlab FRR output.

    netlab remains responsible for BGP sessions, origins, and ordinary policy
    maps. This function only adds profile-specific FRR statements and verifies
    that every target is bound in the transformed output.
    """
    rpki = bool(plan.inventory.get("rpki"))
    rtbh = bool(plan.inventory.get("rtbh"))
    if not rpki and not rtbh:
        if plan.inventory.get("leak_prefixes") or any(
            node.rpki_cache
            or node.rov_reject_invalid
            or node.export_deny_prefixes
            or node.rtbh_import_policy
            or node.ebgp_outbound_route_maps
            for node in plan.nodes
        ):
            raise ValueError("BGP plan contains security fields without a profile")
        return dict(configs)
    if plan.mode != "ebgp" or rpki == rtbh:
        raise ValueError("netlab security overlays require one eBGP security profile")
    nodes = {node.device_name: node for node in plan.nodes}
    transformed_nodes = transformed.get("nodes", {})
    if set(configs) != set(nodes) or set(transformed_nodes) != set(nodes):
        raise ValueError("netlab configs, transformed nodes, and BGP plan differ")

    result = dict(configs)
    for name, node_plan in nodes.items():
        config = result[name]
        model = transformed_nodes[name]
        if (
            model.get("device") != "frr"
            or model.get("bgp", {}).get("as") != node_plan.asn
        ):
            raise ValueError(f"netlab FRR node identity differs from BGP plan: {name}")
        neighbors = model.get("bgp", {}).get("neighbors", [])
        expected_peers = {
            (session.remote_ip, session.remote_asn, session.session_type)
            for session in node_plan.sessions
        }
        actual_peers = {
            (str(row.get("ipv4")), int(row.get("as")), row.get("type"))
            for row in neighbors
        }
        if actual_peers != expected_peers:
            raise ValueError(f"netlab BGP peers differ from plan on {name}")
        expected_origins = {origin.prefix for origin in node_plan.originated}
        actual_origins = {
            str(prefix["ipv4"])
            for prefix in model.get("bgp", {}).get("advertise", [])
            if "ipv4" in prefix
        }
        if actual_origins != expected_origins:
            raise ValueError(f"netlab BGP origins differ from plan on {name}")
        if not neighbors:
            if (
                node_plan.rpki_cache
                or node_plan.rov_reject_invalid
                or node_plan.export_deny_prefixes
                or node_plan.rtbh_import_policy
                or node_plan.ebgp_outbound_route_maps
            ):
                raise ValueError(
                    f"Security overlay has no transformed BGP peers on {name}"
                )
            continue
        bindings = _bindings(config, neighbors, name)
        lines: list[str] = []

        if node_plan.rpki_cache:
            lines.extend(_rpki_stanza(node_plan.rpki_cache))
        if node_plan.rov_reject_invalid:
            maps = sorted({row["in"] for row in bindings.values() if "in" in row})
            for policy in maps:
                _require_map(config, policy, name)
                lines.extend(_rpki_reject_route_map(policy))
        if node_plan.export_deny_prefixes:
            maps = sorted({row["out"] for row in bindings.values() if "out" in row})
            if not maps:
                if any(row.get("type") == "ebgp" for row in neighbors):
                    raise ValueError(
                        f"Export deny overlay has no output policy on {name}"
                    )
            else:
                _require_business_list(config, name)
                lines.extend(_leak_prefix_list(tuple(node_plan.export_deny_prefixes)))
                for policy in maps:
                    _require_map(config, policy, name)
                    lines.extend(
                        [
                            f"route-map {policy} deny 5",
                            f" match ip address prefix-list {EXPORT_DENY_PREFIX_LIST}",
                            "!",
                        ]
                    )
        if node_plan.rtbh_import_policy:
            community = str(plan.inventory.get("blackhole_community") or "")
            discard = str(plan.inventory.get("discard_next_hop") or "")
            if not community or not discard:
                raise ValueError("RTBH plan lacks community or discard next hop")
            _require_business_list(config, name)
            maps = sorted({row["in"] for row in bindings.values() if "in" in row})
            if not maps:
                raise ValueError(f"RTBH provider has no actual import policy on {name}")
            lines.extend(_rtbh_discard_static(discard))
            for policy in maps:
                _require_map(config, policy, name)
                lines.extend(
                    _rtbh_import_route_map(
                        policy,
                        prefix_list="BUSINESS-ipv4",
                        blackhole_community=community,
                        discard_next_hop=discard,
                    )
                )

        if node_plan.ebgp_outbound_route_maps:
            outbound = dict(node_plan.ebgp_outbound_route_maps)
            if len(outbound) != len(node_plan.ebgp_outbound_route_maps):
                raise ValueError(f"Duplicate RTBH outbound peer on {name}")
            for peer, policy in outbound.items():
                neighbor = next(
                    (row for row in neighbors if row.get("ipv4") == peer), None
                )
                if neighbor is None or neighbor.get("type") != "ebgp":
                    raise ValueError(
                        f"RTBH peer is absent from transformed BGP: {name} {peer}"
                    )
                _require_business_list(config, name)
                lines.extend(
                    _leaker_outbound_route_maps(
                        ((peer, policy),), prefix_list="BUSINESS-ipv4"
                    )
                )
                lines.extend(
                    [
                        f"router bgp {node_plan.asn}",
                        " address-family ipv4 unicast",
                        f"  neighbor {peer} route-map {policy} out",
                        " exit-address-family",
                        "!",
                    ]
                )

        if lines:
            result[name] = config.rstrip() + "\n\n" + "\n".join(lines) + "\n"
    return result


def _bindings(
    config: str, neighbors: list[dict], node: str
) -> dict[str, dict[str, str]]:
    actual = {}
    for ip, policy, direction in _BINDING.findall(config):
        actual.setdefault(ip, {})[direction] = policy
    expected = {str(row["ipv4"]) for row in neighbors if row.get("type") == "ebgp"}
    if any(not {"in", "out"} <= set(actual.get(ip, {})) for ip in expected):
        raise ValueError(f"netlab eBGP policy bindings are incomplete on {node}")
    return actual


def _require_map(config: str, policy: str, node: str) -> None:
    if not re.search(
        rf"^route-map {re.escape(policy)} (?:permit|deny) [0-9]+$", config, re.M
    ):
        raise ValueError(f"netlab policy {policy!r} is undefined on {node}")


def _require_business_list(config: str, node: str) -> None:
    if not re.search(r"^ip prefix-list BUSINESS-ipv4\b", config, re.M):
        raise ValueError(f"netlab BUSINESS-ipv4 list missing on {node}")
