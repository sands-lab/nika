"""Render typed ISP plans as RouterOS 7.21.5 auto-import commands."""

from __future__ import annotations

import re
from ipaddress import IPv4Network

from nika.net_env.isp.bgp.plan import BgpNodePlan, BgpPlan
from nika.net_env.isp.bgp.policy import business_prefixes
from nika.net_env.isp.igp.plan import IspPlan


def routeros_interface(name: str) -> str:
    """Translate plan interface names; ether1 remains the management interface."""
    if name == "lo":
        return "lo0"
    if re.fullmatch(r"eth[0-9]+", name):
        return f"ether{int(name[3:]) + 1}"
    raise ValueError(f"Unsupported RouterOS ISP interface {name!r}")


def render_routeros_isp(
    plan: IspPlan, bgp_plan: BgpPlan | None = None
) -> dict[str, str]:
    """Return device-name to startup script, preserving the planned route graph."""
    if plan.igp != "ospf":
        raise ValueError("RouterOS ISP supports only IPv4 OSPF")
    if any(i.name == "eth0" for n in plan.nodes for i in n.interfaces):
        raise ValueError(
            "RouterOS reserves eth0 for management; plan data ports from eth1"
        )
    bgp_nodes = {}
    if bgp_plan is not None:
        if bgp_plan.mode not in {"ibgp_rr", "ebgp"}:
            raise ValueError(f"Unsupported RouterOS BGP mode {bgp_plan.mode!r}")
        if any(
            bgp_plan.inventory.get(key) for key in ("rpki", "rtbh", "leak_prefixes")
        ):
            raise ValueError("RouterOS ISP does not support BGP security profiles")
        bgp_nodes = {node.device_name: node for node in bgp_plan.nodes}
        if set(bgp_nodes) != {node.device_name for node in plan.nodes}:
            raise ValueError("RouterOS BGP and ISP plans must cover the same devices")
    result = {}
    for node in plan.nodes:
        lines = [
            f"/system identity set name={node.device_name}",
            "/interface bridge add name=lo0 protocol-mode=none",
            f"/ip address add address={node.loopback}/32 interface=lo0",
            f"/routing ospf instance add name=NIKA version=2 router-id={node.router_id}",
            "/routing ospf area add name=backbone instance=NIKA area-id=0.0.0.0",
            "/routing ospf interface-template add area=backbone interfaces=lo0 "
            f"networks={node.loopback}/32 type=broadcast passive cost=1",
        ]
        for iface in node.interfaces:
            name = routeros_interface(iface.name)
            lines.extend(
                [
                    f"/ip address add address={iface.address}/{iface.prefixlen} "
                    f"interface={name}",
                    "/routing ospf interface-template add area=backbone "
                    f"interfaces={name} networks={iface.subnet} "
                    f"type={'broadcast' if iface.passive else 'ptp'} "
                    f"cost={iface.metric}{' passive' if iface.passive else ''}",
                ]
            )
        if bgp_plan is not None:
            lines.extend(_render_bgp(bgp_nodes[node.device_name], bgp_plan))
        result[node.device_name] = "\n".join(lines) + "\n"
    return result


def _filter_rule(chain: str, rule: str) -> str:
    return f'/routing filter rule add chain={chain} rule="{rule}"'


def _render_bgp(node: BgpNodePlan, plan: BgpPlan) -> list[str]:
    if (
        node.rov_reject_invalid
        or node.rpki_cache
        or node.export_deny_prefixes
        or node.rtbh_import_policy
        or node.ebgp_outbound_route_maps
    ):
        raise ValueError("RouterOS ISP does not support BGP security profiles")
    lines = []
    if node.originated:
        lines.append("/interface bridge add name=lo1 protocol-mode=none")
    for origin in sorted(node.originated, key=lambda item: item.prefix):
        plen = IPv4Network(origin.prefix).prefixlen
        lines.extend(
            [
                f"/ip address add address={origin.ping_address}/{plen} interface=lo1",
                "/ip firewall address-list add list=BGP-ORIGINS "
                f"address={origin.prefix}",
            ]
        )
    # Match the ordinary FRR BUSINESS policy, including the dormant fault prefix.
    prefixes = business_prefixes(origin.prefix for origin in plan.originated)
    for chain in ("BGP-IN", "BGP-OUT"):
        for prefix in prefixes:
            lines.append(_filter_rule(chain, f"if (dst == {prefix}) {{ accept; }}"))
        lines.append(_filter_rule(chain, "reject;"))
    inbound = {}
    for index, (neighbor, terms) in enumerate(node.import_policies):
        chain = f"NC-IN-{index}"
        inbound[neighbor] = chain
        for term in terms:
            action = "reject;"
            if term.permit:
                action = "jump BGP-IN;"
                if term.local_preference is not None:
                    action = f"set bgp-local-pref {term.local_preference}; {action}"
            lines.append(
                _filter_rule(chain, f"if (dst == {term.prefix}) {{ {action} }}")
            )
        lines.append(_filter_rule(chain, "jump BGP-IN;"))
    instance = (
        f"/routing bgp instance add name=NIKA as={node.asn} router-id={node.router_id}"
    )
    if node.cluster_id:
        instance += f" cluster-id={node.cluster_id}"
    lines.append(instance)
    for index, session in enumerate(
        sorted(node.sessions, key=lambda item: (item.remote_ip, item.remote_device))
    ):
        role = session.session_type
        if session.route_reflector_client:
            role = "ibgp-rr"
        command = (
            f"/routing bgp connection add name=peer-{index} instance=NIKA "
            f"local.role={role} local.address={session.local_ip} "
            f"remote.address={session.remote_ip} remote.as={session.remote_asn} "
            "afi=ip connect=yes listen=yes "
            f"input.filter={inbound.get(session.remote_ip, 'BGP-IN')} "
            "output.filter-chain=BGP-OUT"
        )
        if node.originated:
            command += " output.network=BGP-ORIGINS"
        if plan.mode == "ebgp":
            # Keep reflected next hops unchanged (RFC 4456 section 4).
            # External-link subnets are already advertised passively in OSPF,
            # so unmodified eBGP next hops remain reachable throughout the AS.
            command += " nexthop-choice=default"
        lines.append(command)
    return lines
