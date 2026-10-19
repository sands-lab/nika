"""Render typed ISP routing plans as native IOS XR startup configurations."""

from __future__ import annotations

import re
from ipaddress import IPv4Network

from nika.net_env.isp.bgp.plan import BgpNodePlan, BgpPlan
from nika.net_env.isp.igp.plan import IspPlan


def iosxr_interface(name: str) -> str:
    """Map a planned Linux interface to its XRd control-plane interface."""
    if name == "lo":
        return "Loopback0"
    if match := re.fullmatch(r"eth(0|[1-9][0-9]*)", name):
        return f"GigabitEthernet0/0/0/{match[1]}"
    raise ValueError(f"Unsupported IOS XR ISP interface: {name!r}")


def render_iosxr_isp(plan: IspPlan, bgp_plan: BgpPlan | None = None) -> dict[str, str]:
    """Return device-name/config mappings for IPv4 OSPF and original BGP plans.

    Business origins use separate loopbacks excluded from OSPF. Security profile
    overlays require native policy implementations and are rejected explicitly.
    """
    if plan.igp != "ospf":
        raise ValueError("IOS XR ISP rendering supports OSPF only")
    bgp_nodes = {}
    if bgp_plan is not None:
        if bgp_plan.mode not in ("ibgp_rr", "ebgp"):
            raise ValueError(f"Unsupported IOS XR ISP BGP mode: {bgp_plan.mode!r}")
        if any(
            bgp_plan.inventory.get(key)
            for key in ("rpki", "rtbh", "leak_prefixes", "target_prefix")
        ) or any(
            node.rpki_cache
            or node.rov_reject_invalid
            or node.rtbh_import_policy
            or node.export_deny_prefixes
            or node.ebgp_outbound_route_maps
            for node in bgp_plan.nodes
        ):
            raise ValueError(
                "IOS XR ISP rendering does not support RPKI, RTBH, "
                "or export-restricted BGP profiles"
            )
        bgp_nodes = {node.device_name: node for node in bgp_plan.nodes}
        if set(bgp_nodes) != {node.device_name for node in plan.nodes}:
            raise ValueError("IOS XR ISP BGP nodes must match the IGP plan")

    configurations = {}
    for node in plan.nodes:
        lines = [
            f"hostname {node.device_name}",
            "!",
            "interface Loopback0",
            f" ipv4 address {node.loopback}/32",
            " no shutdown",
            "!",
        ]
        for interface in node.interfaces:
            lines.extend(
                [
                    f"interface {iosxr_interface(interface.name)}",
                    f" ipv4 address {interface.address}/{interface.prefixlen}",
                    " no shutdown",
                    "!",
                ]
            )
        lines.extend(
            [
                "router ospf NIKA",
                f" router-id {node.router_id}",
                " area 0",
                "  interface Loopback0",
                "   passive enable",
                "  !",
            ]
        )
        for interface in node.interfaces:
            lines.extend(
                [
                    f"  interface {iosxr_interface(interface.name)}",
                    f"   cost {interface.metric}",
                ]
            )
            if interface.passive:
                lines.append("   passive enable")
            else:
                # Backbone links are dedicated /31 point-to-point circuits.
                lines.append("   network point-to-point")
            lines.append("  !")
        lines.extend([" !", "!"])
        if bgp_plan is not None:
            lines.extend(_render_bgp(bgp_nodes[node.device_name], bgp_plan))
        lines.extend(["end", ""])
        configurations[node.device_name] = "\n".join(lines)
    return configurations


def _render_bgp(node: BgpNodePlan, plan: BgpPlan) -> list[str]:
    lines = []
    for index, origin in enumerate(sorted(node.originated, key=lambda p: p.prefix), 1):
        prefix = IPv4Network(origin.prefix)
        lines.extend(
            [
                f"interface Loopback{index}",
                f" ipv4 address {origin.ping_address}/{prefix.prefixlen}",
                " no shutdown",
                "!",
            ]
        )
    # Match the original FRR BUSINESS allow-list, including the dormant prefix
    # used by unauthorized-origin failures and additional planned origins.
    business = {
        "203.0.113.0/24",
        "203.0.114.0/24",
        "203.0.115.0/24",
        "198.51.100.0/24",
        "198.51.101.0/24",
        "198.51.102.0/24",
        "198.18.0.0/24",
        *(origin.prefix for origin in plan.originated),
    }
    lines.append("prefix-set BUSINESS")
    prefixes = sorted(business)
    lines.extend(
        f" {prefix}{',' if i < len(prefixes) - 1 else ''}"
        for i, prefix in enumerate(prefixes)
    )
    lines.extend(["end-set", "!"])
    for name in ("BGP-IN", "BGP-OUT"):
        lines.extend(
            [
                f"route-policy {name}",
                " if destination in BUSINESS then",
                "  pass",
                " else",
                "  drop",
                " endif",
                "end-policy",
                "!",
            ]
        )
    inbound = {}
    for index, (neighbor, terms) in enumerate(node.import_policies):
        name = f"NC-IN-{index}"
        inbound[neighbor] = name
        lines.append(f"route-policy {name}")
        for seq, term in enumerate(terms):
            branch = "if" if seq == 0 else "elseif"
            lines.append(f" {branch} destination in ({term.prefix}) then")
            if term.permit:
                if term.local_preference is not None:
                    lines.append(f"  set local-preference {term.local_preference}")
                lines.append("  apply BGP-IN")
            else:
                lines.append("  drop")
        if terms:
            lines.extend([" else", "  apply BGP-IN", " endif"])
        else:
            lines.append(" apply BGP-IN")
        lines.extend(["end-policy", "!"])
    lines.extend([f"router bgp {node.asn}", f" bgp router-id {node.router_id}"])
    if node.cluster_id:
        lines.append(f" bgp cluster-id {node.cluster_id}")
    lines.append(" address-family ipv4 unicast")
    for origin in sorted(node.originated, key=lambda p: p.prefix):
        lines.append(f"  network {origin.prefix}")
    lines.append(" !")
    for session in sorted(node.sessions, key=lambda s: (s.remote_ip, s.remote_device)):
        lines.extend(
            [
                f" neighbor {session.remote_ip}",
                f"  remote-as {session.remote_asn}",
            ]
        )
        if session.update_source:
            lines.append(f"  update-source {iosxr_interface(session.update_source)}")
        lines.append("  address-family ipv4 unicast")
        if session.route_reflector_client:
            lines.append("   route-reflector-client")
        # Preserve reflected next hops (RFC 4456 section 4). Passive OSPF
        # advertisements already make the external-link subnets reachable.
        lines.extend(
            [
                f"   route-policy {inbound.get(session.remote_ip, 'BGP-IN')} in",
                "   route-policy BGP-OUT out",
                "  !",
                " !",
            ]
        )
    lines.append("!")
    return lines
