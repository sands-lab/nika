"""Render typed ISP plans as RouterOS 7.21.5 auto-import commands."""

from __future__ import annotations

from ipaddress import IPv4Network

from nika.net_env.isp.bgp.plan import BgpNodePlan, BgpPlan
from nika.net_env.isp.bgp.policy import BUSINESS_PREFIXES
from nika.net_env.isp.igp.frr import isis_net_from_router_id
from nika.net_env.isp.igp.plan import IspPlan, PlannedNode, active_igp_links
from nika.net_env.utils.routeros.common import MAX_DATA_INTERFACES, routeros_interface

__all__ = ["render_routeros_isp", "routeros_interface", "routeros_isp_readiness"]

SUPPORTED_IGPS = ("ospf", "isis")


def render_routeros_isp(
    plan: IspPlan, bgp_plan: BgpPlan | None = None
) -> dict[str, str]:
    """Return device-name to startup script, preserving the planned route graph."""
    if plan.igp not in SUPPORTED_IGPS:
        raise ValueError(
            f"RouterOS ISP supports IPv4 {' and '.join(SUPPORTED_IGPS)}, "
            f"not {plan.igp!r}"
        )
    if any(i.name == "eth0" for n in plan.nodes for i in n.interfaces):
        raise ValueError(
            "RouterOS reserves eth0 for management; plan data ports from eth1"
        )
    if oversized := [
        n.device_name for n in plan.nodes if len(n.interfaces) > MAX_DATA_INTERFACES
    ]:
        raise ValueError(
            f"RouterOS routers support at most {MAX_DATA_INTERFACES} data "
            f"interfaces; {', '.join(sorted(oversized))} need more"
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
            *(
                f"/ip address add address={iface.address}/{iface.prefixlen} "
                f"interface={routeros_interface(iface.name)}"
                for iface in node.interfaces
            ),
            *(_render_ospf(node) if plan.igp == "ospf" else _render_isis(node)),
        ]
        if bgp_plan is not None:
            lines.extend(_render_bgp(bgp_nodes[node.device_name], bgp_plan))
        result[node.device_name] = "\n".join(lines) + "\n"
    return result


def _render_ospf(node: PlannedNode) -> list[str]:
    lines = [
        f"/routing ospf instance add name=NIKA version=2 router-id={node.router_id}",
        "/routing ospf area add name=backbone instance=NIKA area-id=0.0.0.0",
        "/routing ospf interface-template add area=backbone interfaces=lo0 "
        f"networks={node.loopback}/32 type=broadcast passive cost=1",
    ]
    for iface in node.interfaces:
        lines.append(
            "/routing ospf interface-template add area=backbone "
            f"interfaces={routeros_interface(iface.name)} networks={iface.subnet} "
            f"type={'broadcast' if iface.passive else 'ptp'} "
            f"cost={iface.metric}{' passive' if iface.passive else ''}"
        )
    return lines


def isis_identity(router_id: str) -> tuple[str, str]:
    """Return the (area, system-id) pair of the FRR NET for ``router_id``."""
    parts = isis_net_from_router_id(router_id).split(".")
    return ".".join(parts[:2]), ".".join(parts[2:5])


def _render_isis(node: PlannedNode) -> list[str]:
    # Match FRR: one level-2 domain, wide metrics, point-to-point backbone
    # circuits, and passive loopback, edge and AS-boundary interfaces.
    area, system_id = isis_identity(node.router_id)
    lines = [
        f"/routing isis instance add name=NIKA afi=ip areas={area} "
        f"system-id={system_id} metric-type=wide",
        "/routing isis interface-template add instance=NIKA interfaces=lo0 "
        "levels=l2 passive",
    ]
    for iface in node.interfaces:
        name = routeros_interface(iface.name)
        # RouterOS passive circuits take no metric and advertise their prefix
        # with metric 0. Each passive prefix has one origin, so path
        # selection matches FRR; only the reported route metric differs.
        circuit = "passive" if iface.passive else f"ptp ptp.l2.metric={iface.metric}"
        lines.append(
            "/routing isis interface-template add instance=NIKA "
            f"interfaces={name} levels=l2 {circuit}"
        )
    return lines


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
    prefixes = {
        *BUSINESS_PREFIXES,
        *(origin.prefix for origin in plan.originated),
    }
    for chain in ("BGP-IN", "BGP-OUT"):
        for prefix in sorted(prefixes):
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
            # External-link subnets are already advertised passively in the IGP,
            # so unmodified eBGP next hops remain reachable throughout the AS.
            command += " nexthop-choice=default"
        lines.append(command)
    return lines


def _bgp_route_count(prefix: str, *, exact: bool) -> str:
    match = f"dst-address={prefix}" if exact else f"dst-address in {prefix}"
    # Routes rejected by an input filter stay visible as filtered candidates.
    return f"/routing/route/print count-only where bgp and !filtered and {match}"


def routeros_isp_readiness(
    runtime, plan: IspPlan, bgp_plan: BgpPlan | None = None, *, full: bool = False
) -> dict[str, bool]:
    """Return RouterOS checks that the shared ISP contract does not express.

    Every router must have imported its startup script; a rejected script
    raises immediately instead of waiting for the readiness deadline. IS-IS
    adjacencies are checked per planned link because the shared contract
    declares IGP adjacencies only for OSPF. As on FRR, readiness requires
    every expected BGP prefix at its observer; the full check also requires
    that no BGP speaker accepted an infrastructure prefix.
    """
    from nika.net_env.utils.routeros.common import (
        CONFIG_LOG_COMMAND,
        routeros_config_applied,
    )
    from nika.net_env.utils.routeros.parse import routeros_terse_rows
    from nika.net_env.verify_collection import VerifyCommand, collect_verify_commands
    from nika.service.lab.routeros_api import CLI_COMMAND

    neighbors = CLI_COMMAND.format(
        command="/routing/isis/neighbor/print terse where state=up"
    )
    commands = [VerifyCommand(n.device_name, CONFIG_LOG_COMMAND) for n in plan.nodes]
    if plan.igp == "isis":
        commands.extend(VerifyCommand(n.device_name, neighbors) for n in plan.nodes)
    route_counts: dict[str, list[tuple[str, str]]] = {}
    if bgp_plan is not None:
        route_counts["bgp_prefixes_propagated"] = [
            (observer, CLI_COMMAND.format(command=_bgp_route_count(p, exact=True)))
            for observer, p in bgp_plan.expect_reachable
        ]
        if full:
            speakers = sorted({s.local_device for s in bgp_plan.sessions})
            route_counts["bgp_infra_denied"] = [
                (node, CLI_COMMAND.format(command=_bgp_route_count(p, exact=False)))
                for node in speakers
                for p in bgp_plan.deny_prefixes
            ]
    commands.extend(
        VerifyCommand(node, command)
        for queries in route_counts.values()
        for node, command in queries
    )
    collected = collect_verify_commands(runtime, commands)
    checks = {
        "routeros_config_applied": routeros_config_applied(
            (n.device_name, collected.exec(n.device_name, CONFIG_LOG_COMMAND))
            for n in plan.nodes
        )
    }

    def counts(queries: list[tuple[str, str]]) -> list[int | None]:
        values = [collected.exec(node, command).strip() for node, command in queries]
        return [int(value) if value.isdigit() else None for value in values]

    if "bgp_prefixes_propagated" in route_counts:
        checks["bgp_prefixes_propagated"] = all(
            count is not None and count > 0
            for count in counts(route_counts["bgp_prefixes_propagated"])
        )
    if "bgp_infra_denied" in route_counts:
        checks["bgp_infra_denied"] = all(
            count == 0 for count in counts(route_counts["bgp_infra_denied"])
        )
    if plan.igp == "isis":
        system_ids = {n.device_name: isis_identity(n.router_id)[1] for n in plan.nodes}
        active = {link.link_id for link in active_igp_links(plan)}

        def node_ok(node: PlannedNode) -> bool:
            try:
                rows = routeros_terse_rows(collected.exec(node.device_name, neighbors))
            except ValueError:
                return False
            up = {(row.get("interface"), row.get("srcid")) for row in rows}
            return all(
                (routeros_interface(i.name), system_ids[i.peer_device]) in up
                for i in node.interfaces
                if i.link_id in active and not i.passive
            )

        checks["isis_adjacencies"] = all(node_ok(node) for node in plan.nodes)
    return checks
