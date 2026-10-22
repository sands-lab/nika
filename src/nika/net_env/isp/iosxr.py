"""Render typed ISP routing plans as native IOS XR startup configurations."""

from __future__ import annotations

import re
from ipaddress import IPv4Network

from nika.net_env.isp.bgp.plan import BgpNodePlan, BgpPlan
from nika.net_env.isp.bgp.policy import business_prefixes
from nika.net_env.isp.igp.frr import ISIS_PROCESS, isis_net_from_router_id
from nika.net_env.isp.igp.plan import IspPlan, PlannedNode
from nika.net_env.utils.iosxr.common import iosxr_interface


def render_iosxr_isp(plan: IspPlan, bgp_plan: BgpPlan | None = None) -> dict[str, str]:
    """Return device-name/config mappings for IPv4 OSPF or IS-IS and BGP plans.

    Business origins use separate loopbacks excluded from the IGP. Security
    profile overlays require native policy implementations and are rejected.
    """
    if plan.igp not in {"ospf", "isis"}:
        raise ValueError("IOS XR ISP rendering supports OSPF and IS-IS only")
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
        if bgp_plan is not None:
            lines.extend(_origin_interfaces(bgp_nodes[node.device_name]))
        # XR resolves a command in the innermost mode that accepts it, so all
        # interfaces precede routing blocks that also have interface submodes.
        render_igp = _render_ospf if plan.igp == "ospf" else _render_isis
        lines.extend(render_igp(node))
        if bgp_plan is not None:
            lines.extend(_render_bgp(bgp_nodes[node.device_name], bgp_plan))
        lines.extend(["end", ""])
        configurations[node.device_name] = "\n".join(lines)
    return configurations


def _render_ospf(node: PlannedNode) -> list[str]:
    lines = [
        "router ospf CORE",
        f" router-id {node.router_id}",
        " area 0",
        "  interface Loopback0",
        "   passive enable",
        "  !",
    ]
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
    return lines


def _render_isis(node: PlannedNode) -> list[str]:
    """Render the FRR ISP IS-IS design: one Level-2 instance, wide metrics."""
    lines = [
        f"router isis {ISIS_PROCESS}",
        " is-type level-2-only",
        f" net {isis_net_from_router_id(node.router_id)}",
        " address-family ipv4 unicast",
        "  metric-style wide",
        " !",
        " interface Loopback0",
        "  passive",
        "  address-family ipv4 unicast",
        "  !",
        " !",
    ]
    for interface in node.interfaces:
        lines.append(f" interface {iosxr_interface(interface.name)}")
        if interface.passive:
            # Edge and inter-AS subnets are advertised without adjacencies.
            lines.append("  passive")
        else:
            # Backbone links are dedicated /31 point-to-point circuits.
            lines.extend(["  circuit-type level-2-only", "  point-to-point"])
        lines.extend(
            [
                "  address-family ipv4 unicast",
                f"   metric {interface.metric}",
                "  !",
                " !",
            ]
        )
    lines.append("!")
    return lines


def _origin_interfaces(node: BgpNodePlan) -> list[str]:
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
    return lines


def _render_bgp(node: BgpNodePlan, plan: BgpPlan) -> list[str]:
    lines = []
    # Match the original FRR BUSINESS allow-list, including the dormant prefix
    # used by unauthorized-origin failures and additional planned origins.
    lines.append("prefix-set BUSINESS")
    prefixes = business_prefixes(origin.prefix for origin in plan.originated)
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


ISIS_ADJACENCY_COMMAND = "/pkg/bin/xr_cli 'show isis adjacency'"
_XR_INTERFACE_ABBREVIATIONS = {"Gi": "GigabitEthernet"}


def parse_iosxr_isis_adjacencies(output: str) -> set[tuple[str, str]]:
    """Return ``(system id, interface)`` pairs of Up Level-2 adjacencies.

    A Level-2 table with no rows means no neighbors. Output without the table
    is a CLI or parser error and must not be read as a down adjacency.
    """
    if not re.search(r"IS-IS \S+ Level-2 adjacencies:", output):
        if re.search(r"Total adjacency count:\s*0|No IS-IS", output):
            return set()
        raise ValueError("IOS XR returned no recognizable IS-IS adjacency table")
    adjacencies = set()
    for line in output.split("Level-2 adjacencies:", 1)[1].splitlines():
        fields = line.split()
        if len(fields) < 4 or fields[0] in {"System", "BFD", "Total"}:
            continue
        match = re.fullmatch(r"([A-Za-z]+)(\d+(?:/\d+)*)", fields[1])
        if match is None:
            raise ValueError(f"Malformed IOS XR IS-IS adjacency row: {line.strip()}")
        prefix = _XR_INTERFACE_ABBREVIATIONS.get(match[1], match[1])
        if fields[3] == "Up":
            adjacencies.add((fields[0], f"{prefix}{match[2]}"))
    return adjacencies


def iosxr_isis_adjacency_checks(observed, plan: IspPlan) -> dict[str, bool]:
    """Check every active IGP link for an Up Level-2 adjacency at both ends.

    ``observed`` holds :data:`ISIS_ADJACENCY_COMMAND` results for each router.
    XR reports neighbors by dynamic hostname, which is the planned device name.
    """
    from nika.net_env.isp.igp.plan import active_igp_links

    tables: dict[str, set[tuple[str, str]] | None] = {}
    for node in plan.nodes:
        result = observed.results.get((node.device_name, ISIS_ADJACENCY_COMMAND))
        try:
            if result is None or result.error or result.returncode != 0:
                raise ValueError("IS-IS adjacency query failed")
            tables[node.device_name] = parse_iosxr_isis_adjacencies(result.output)
        except ValueError:
            tables[node.device_name] = None
    checks = {}
    for link in active_igp_links(plan):
        for local, interface, remote in (
            (link.endpoint_a, link.iface_a, link.endpoint_b),
            (link.endpoint_b, link.iface_b, link.endpoint_a),
        ):
            table = tables[local]
            checks[f"adj.isis.{local}.{remote}.{interface}"] = bool(
                table and (remote, iosxr_interface(interface)) in table
            )
    return checks
