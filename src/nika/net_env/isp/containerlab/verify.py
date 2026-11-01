"""Startup verification for Containerlab ISP (Nokia SR Linux)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.igp.plan import (
    IspPlan,
    PlannedNode,
    active_igp_links,
    igp_components,
)
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.net_env.isp.verify_support import sample_traffic
from nika.net_env.isp.fib import SRL_FIB_COMMAND, srl_fib, verify_fib
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    nodes_deployed,
    ping_unreachable,
    srl_bgp_established_peers,
)
from nika.utils.parallel import bounded_parallel_map
from nika.runtime.base import LabRuntime
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    mesh_ping_command,
)


# SR Linux runs its data plane in this namespace; the container's default
# namespace only has the management interface.
_SRL_DATA_NETNS = "srbase-default"


def _collect(runtime, plan, bgp_plan, traffic, *, full):
    commands = []
    adjacency = (
        'sr_cli "show network-instance default protocols isis adjacency"'
        if plan.igp == "isis"
        else 'sr_cli "show network-instance default protocols ospf neighbor"'
    )
    for node in plan.nodes:
        commands.append(VerifyCommand(node.device_name, adjacency, 30))
        if full:
            commands.extend(
                [
                    VerifyCommand(node.device_name, SRL_FIB_COMMAND, 30),
                    VerifyCommand(node.device_name, 'sr_cli "show interface system0"'),
                ]
            )
        if bgp_plan:
            commands.append(
                VerifyCommand(
                    node.device_name,
                    'sr_cli "show network-instance default protocols bgp neighbor"',
                    30,
                )
            )
            if full:
                commands.append(
                    VerifyCommand(
                        node.device_name,
                        'sr_cli "show network-instance default route-table ipv4-unicast summary"',
                        30,
                    )
                )
    for source, targets, netns in _loopback_probes(plan, traffic):
        commands.extend(
            VerifyCommand(source, mesh_ping_command(target, count=1, netns=netns), 30)
            for target in targets
        )
    if full and traffic:
        for host in traffic.hosts:
            commands.extend(
                [
                    VerifyCommand(
                        host.host_name, f"ip -4 -o addr show dev {host.host_iface}", 10
                    ),
                    VerifyCommand(host.host_name, mesh_ping_command(host.gateway), 30),
                ]
            )
        by_router = {host.router_device: host for host in traffic.hosts}
        for component in igp_components(plan):
            hosts = [by_router[router] for router in component if router in by_router]
            if len(hosts) >= 2:
                commands.extend(
                    VerifyCommand(
                        hosts[0].host_name, mesh_ping_command(host.address), 30
                    )
                    for host in hosts[1:]
                )
    if full and bgp_plan:
        for origin in bgp_plan.originated:
            commands.append(
                VerifyCommand(
                    origin.device,
                    f'sr_cli "show network-instance default protocols bgp routes ipv4 prefix {origin.prefix}"',
                    30,
                )
            )
        origins = {origin.prefix: origin for origin in bgp_plan.originated}
        for observer, prefix in bgp_plan.expect_reachable:
            commands.append(
                VerifyCommand(
                    observer,
                    f'sr_cli "show network-instance default protocols bgp routes ipv4 prefix {prefix}"',
                    30,
                )
            )
            origin = origins.get(prefix)
            if origin:
                source = next(
                    (
                        session.local_ip
                        for session in bgp_plan.sessions
                        if session.session_type == "ebgp"
                        and session.local_device == observer
                        and session.remote_device == origin.device
                    ),
                    None,
                )
                source_arg = f" -I {source}" if source else ""
                commands.append(
                    VerifyCommand(
                        observer,
                        f'sr_cli "ping {origin.ping_address} network-instance default -c 3{source_arg}"',
                    )
                )
    return collect_verify_commands(runtime, commands)


def verify_isp_srl_lab_startup(
    runtime: LabRuntime,
    *,
    plan: IspPlan,
    scenario_name: str,
    bgp_plan: BgpPlan | None = None,
    traffic: IspTrafficAttachment | None = None,
) -> dict[str, Any]:
    expected = [node.device_name for node in plan.nodes]
    if traffic is not None:
        expected.extend(h.host_name for h in traffic.hosts)
    traffic = sample_traffic(traffic, bgp_plan=bgp_plan)
    runtime = _collect(runtime, plan, bgp_plan, traffic, full=False)
    checks: dict[str, bool] = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "igp_adjacencies": _igp_adjacencies_ok(runtime, plan),
        # Adjacencies can be up before SPF installs routes to every loopback.
        "loopbacks_reachable": _loopbacks_reachable(runtime, plan, traffic),
    }
    if bgp_plan is not None:
        checks["bgp_sessions"] = _bgp_sessions_ok(runtime, bgp_plan)
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={
                "topology_name": plan.topology_name,
                "igp": plan.igp,
                "bgp_mode": bgp_plan.mode if bgp_plan is not None else "none",
            },
        )
    )


def verify_isp_srl_lab(
    runtime: LabRuntime,
    *,
    plan: IspPlan,
    scenario_name: str,
    bgp_plan: BgpPlan | None = None,
    traffic: IspTrafficAttachment | None = None,
) -> dict[str, Any]:
    expected = [node.device_name for node in plan.nodes]
    if traffic is not None:
        expected.extend(h.host_name for h in traffic.hosts)
    all_hosts = [host.host_name for host in traffic.hosts] if traffic else []
    traffic = sample_traffic(traffic, bgp_plan=bgp_plan)
    runtime = _collect(runtime, plan, bgp_plan, traffic, full=True)
    checks: dict[str, bool] = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "igp_adjacencies": _igp_adjacencies_ok(runtime, plan),
        "loopbacks_reachable": _loopbacks_reachable(runtime, plan, traffic),
        "inventory_addresses": _inventory_addresses_ok(runtime, plan),
    }
    details: dict[str, Any] = {
        "topology_name": plan.topology_name,
        "igp": plan.igp,
        "node_count": len(plan.nodes),
        "link_count": len(plan.links),
        "inventory": plan.inventory,
        "bgp_mode": bgp_plan.mode if bgp_plan is not None else "none",
        "device_profile": "nokia_srlinux",
        "traffic_stubs": bool(
            (plan.inventory.get("traffic") or {}).get("stubs")
            or plan.inventory.get("hosts")
        ),
    }
    if traffic is not None:
        checks["stub_hosts_addressed"] = _stub_hosts_addressed_ok(runtime, traffic)
        checks["stub_gateway_reachable"] = _stub_gateway_ok(runtime, traffic)
        checks["stub_remote_reachable"] = _stub_remote_ok(runtime, traffic)
        details["hosts"] = plan.inventory.get("hosts")
    if bgp_plan is not None:
        checks["bgp_sessions"] = _bgp_sessions_ok(runtime, bgp_plan)
        checks["bgp_prefixes_originated"] = _bgp_prefixes_originated_ok(
            runtime, bgp_plan
        )
        checks["bgp_routes_installed"] = _bgp_routes_installed_ok(runtime, bgp_plan)
        propagated, prefix_probes = _bgp_prefixes_propagated_ok(runtime, bgp_plan)
        checks["bgp_prefixes_propagated"] = propagated
        details["bgp"] = bgp_plan.inventory
        details["bgp_prefix_probes"] = prefix_probes
    fibs, fib_errors = {}, {}
    for node in plan.nodes:
        try:
            fibs[node.device_name] = srl_fib(
                runtime.json(node.device_name, SRL_FIB_COMMAND)
            )
        except (ValueError, KeyError, TypeError) as exc:
            fib_errors[node.device_name] = str(exc)
    fib_report = verify_fib(plan, fibs, errors=fib_errors)
    checks["fib_all_pairs_reachable"] = fib_report["all_pairs_reachable"]
    checks["fib_shortest_paths"] = fib_report["shortest_paths"]
    details["fib"] = fib_report
    details["host_coverage"] = {
        "total": len(all_hosts),
        "checked": [host.host_name for host in traffic.hosts] if traffic else [],
        "selection": "component endpoints and explicit observers",
    }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details=details,
        )
    )


def _igp_adjacencies_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    if not plan.links:
        return True
    if plan.igp not in {"isis", "ospf"}:
        return False
    active = {link.link_id for link in active_igp_links(plan)}
    router_id = {node.device_name: node.router_id for node in plan.nodes}
    command = (
        'sr_cli "show network-instance default protocols isis adjacency"'
        if plan.igp == "isis"
        else 'sr_cli "show network-instance default protocols ospf neighbor"'
    )
    # IS-IS adjacencies are "up"; OSPF adjacencies must reach "full" (2-Way
    # means no database exchange, so no routes).
    up_state = "up" if plan.igp == "isis" else "full"

    def node_ok(node: PlannedNode) -> bool:
        peers = [
            (iface.peer_address, router_id.get(iface.peer_device, ""))
            for iface in node.interfaces
            if iface.link_id in active and not iface.passive
        ]
        if not peers:
            return True
        output = exec_or_empty(runtime, node.device_name, command, timeout=30)
        rows = _srl_rows_in_state(output, up_state)
        cells = set().union(*rows) if rows else set()
        return len(rows) >= len(peers) and all(
            address in cells or rid in cells for address, rid in peers
        )

    return all(bounded_parallel_map(node_ok, plan.nodes))


def _srl_rows_in_state(output: str, state: str) -> list[set[str]]:
    """Return the tokens of SR Linux table rows that contain the token ``state``.

    IS-IS tables separate columns with ``|``; OSPF neighbor rows only box the
    whole row, so rows are split on both.
    """
    rows = []
    for line in output.splitlines():
        if not line.startswith("|"):
            continue
        tokens = set(line.replace("|", " ").lower().split())
        if state in tokens:
            rows.append(tokens)
    return rows


def _loopback_probes(plan, traffic, router_netns=_SRL_DATA_NETNS):
    loopbacks = {node.device_name: node.loopback for node in plan.nodes}
    hosts = (
        {host.router_device: host.host_name for host in traffic.hosts}
        if traffic
        else {}
    )
    return [
        (
            hosts.get(component[0], component[0]),
            [loopbacks[router] for router in component],
            None if component[0] in hosts else router_netns,
        )
        for component in igp_components(plan)
    ]


def _loopbacks_reachable(runtime, plan, traffic, router_netns=_SRL_DATA_NETNS):
    return not any(
        ping_unreachable(runtime, source, targets, count=1, netns=netns)
        for source, targets, netns in _loopback_probes(plan, traffic, router_netns)
    )


def _inventory_addresses_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    # Confirm loopback present via sr_cli interface summary.
    def node_ok(node) -> bool:
        output = exec_or_empty(
            runtime,
            node.device_name,
            'sr_cli "show interface system0"',
            timeout=20,
        )
        return node.loopback in output

    return all(bounded_parallel_map(node_ok, plan.nodes))


def _stub_hosts_addressed_ok(
    runtime: LabRuntime, traffic: IspTrafficAttachment
) -> bool:
    for host in traffic.hosts:
        if not host_has_ipv4(
            runtime, host.host_name, host.address, intf=host.host_iface
        ):
            return False
    return True


def _stub_gateway_ok(runtime: LabRuntime, traffic: IspTrafficAttachment) -> bool:
    return not any(
        bounded_parallel_map(
            lambda host: ping_unreachable(runtime, host.host_name, [host.gateway]),
            traffic.hosts,
        )
    )


def _stub_remote_ok(runtime, traffic):
    by_router = {host.router_device: host for host in traffic.hosts}
    for component in igp_components(traffic.plan):
        hosts = [by_router[router] for router in component if router in by_router]
        if len(hosts) >= 2 and ping_unreachable(
            runtime, hosts[0].host_name, [host.address for host in hosts[1:]]
        ):
            return False
    return True


def _bgp_sessions_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    needed: dict[str, set[str]] = defaultdict(set)
    for sess in bgp_plan.sessions:
        needed[sess.local_device].add(sess.remote_ip)

    def device_ok(item: tuple[str, set[str]]) -> bool:
        device, peers = item
        output = exec_or_empty(
            runtime,
            device,
            'sr_cli "show network-instance default protocols bgp neighbor"',
            timeout=30,
        )
        return peers <= srl_bgp_established_peers(output)

    return all(bounded_parallel_map(device_ok, needed.items()))


def _bgp_prefixes_originated_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    """Each originator advertises its prefix to every planned BGP peer.

    SR Linux lists local routes in its BGP RIB whether or not export policy
    lets them out, so the check reads the advertised-to list instead.
    """
    peers: dict[str, set[str]] = defaultdict(set)
    for session in bgp_plan.sessions:
        peers[session.local_device].add(session.remote_ip)

    def advertised(pref) -> bool:
        output = exec_or_empty(
            runtime,
            pref.device,
            'sr_cli "show network-instance default protocols bgp routes ipv4 '
            f'prefix {pref.prefix}"',
            timeout=30,
        )
        lines = output.splitlines()
        sent: set[str] = set()
        for index, line in enumerate(lines[:-1]):
            if "was advertised to" in line:
                sent.update(lines[index + 1].strip("[] ").replace(",", " ").split())
        return bool(peers[pref.device]) and peers[pref.device] <= sent

    return all(bounded_parallel_map(advertised, bgp_plan.originated))


def _bgp_routes_installed_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    """Every BGP speaker installs every other speaker's prefix as an active route."""
    devices = sorted({session.local_device for session in bgp_plan.sessions})

    def device_ok(device: str) -> bool:
        wanted = {p.prefix for p in bgp_plan.originated if p.device != device}
        if not wanted:
            return True
        output = exec_or_empty(
            runtime,
            device,
            'sr_cli "show network-instance default route-table ipv4-unicast summary"',
            timeout=30,
        )
        active = set()
        for line in output.splitlines():
            cells = [cell.strip() for cell in line.split("|")[1:-1]]
            if len(cells) > 4 and cells[2] == "bgp" and cells[4] == "True":
                active.add(cells[0])
        return wanted <= active

    return all(bounded_parallel_map(device_ok, devices))


def _srl_ping_ok(
    runtime: LabRuntime, device: str, target: str, source: str | None = None
) -> bool:
    """Ping from an SRL router via sr_cli (linux ping has no data-plane netns)."""
    source_arg = f" -I {source}" if source else ""
    output = exec_or_empty(
        runtime,
        device,
        f'sr_cli "ping {target} network-instance default -c 3{source_arg}"',
        timeout=20,
    )
    return "3 received" in output or "3 packets received" in output


def _srl_bgp_prefix_ok(runtime: LabRuntime, device: str, prefix: str) -> bool:
    network = prefix.split("/")[0]
    output = exec_or_empty(
        runtime,
        device,
        f'sr_cli "show network-instance default protocols bgp routes ipv4 prefix {prefix}"',
        timeout=30,
    )
    if "Network not in table" in output or "Unknown command" in output:
        return False
    return network in output or prefix in output


def _bgp_prefixes_propagated_ok(
    runtime: LabRuntime, bgp_plan: BgpPlan
) -> tuple[bool, list[dict[str, Any]]]:
    origin_by_prefix = {o.prefix: o for o in bgp_plan.originated}
    probes: list[dict[str, Any]] = []
    for observer, prefix in bgp_plan.expect_reachable:
        route_present = _srl_bgp_prefix_ok(runtime, observer, prefix)
        origin = origin_by_prefix.get(prefix)
        # SRL sources pings from system0, which eBGP export policy keeps out
        # of the peer AS; the eBGP link address is reachable from the origin.
        source = next(
            (
                session.local_ip
                for session in bgp_plan.sessions
                if origin is not None
                and session.session_type == "ebgp"
                and session.local_device == observer
                and session.remote_device == origin.device
            ),
            None,
        )
        ping_reachable = (
            _srl_ping_ok(runtime, observer, origin.ping_address, source)
            if origin
            else None
        )
        probes.append(
            {
                "observer": observer,
                "prefix": prefix,
                "route_present": route_present,
                "ping_reachable": ping_reachable,
            }
        )
    return all(
        probe["route_present"] and probe["ping_reachable"] is not False
        for probe in probes
    ), probes
