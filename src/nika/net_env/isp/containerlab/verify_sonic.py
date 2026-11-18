"""Startup verification for Containerlab ISP on SONiC.

SONiC routes with FRR and forwards in the Linux kernel, so the routing
checks read the same FRR state as the Kathara ISP verifier. SONiC-specific
checks confirm its services run, CONFIG_DB addresses reach the kernel, and
FRR routes reach APPL_DB through FPM.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from ipaddress import ip_network
from typing import Any

from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.containerlab import verify as clab
from nika.net_env.isp.fib import LINUX_FIB_COMMAND, linux_fib, verify_fib
from nika.net_env.isp.igp.plan import IspPlan, igp_components
from nika.net_env.isp.kathara import verify as frr
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.net_env.isp.verify_support import sample_traffic
from nika.net_env.utils.sonic.common import BOOT_SERVICES
from nika.net_env.utils.sonic.render import SonicRouter
from nika.net_env.verify import build_lab_verify_result, nodes_deployed
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    mesh_ping_command,
    ping_command,
)
from nika.runtime.base import LabRuntime

_SERVICES = BOOT_SERVICES
_ADDRESSES = "ip -j -4 addr show"
_APPL_ROUTES = "redis-cli -n 0 --raw keys 'ROUTE_TABLE:*'"
_IGP_NEIGHBORS = {
    "isis": "vtysh -c 'show isis neighbor'",
    "ospf": "vtysh -c 'show ip ospf neighbor'",
}


def _services_command(router: SonicRouter) -> str:
    return "supervisorctl status " + " ".join([*_SERVICES, *router.daemons])


def _collect(runtime, plan, bgp_plan, traffic, routers, *, full):
    commands = []
    for node in plan.nodes:
        device = node.device_name
        commands.append(VerifyCommand(device, _services_command(routers[device])))
        commands.append(VerifyCommand(device, _IGP_NEIGHBORS[plan.igp], 20))
        if bgp_plan:
            commands.extend(
                [
                    VerifyCommand(device, "vtysh -c 'show bgp summary json'", 20),
                    VerifyCommand(device, frr._BGP_TABLE, 30),
                ]
            )
        if full:
            commands.extend(
                VerifyCommand(device, command, 30)
                for command in (_ADDRESSES, LINUX_FIB_COMMAND, _APPL_ROUTES)
            )
    for source, targets, _ in clab._loopback_probes(plan, traffic, None):
        commands.extend(
            VerifyCommand(source, mesh_ping_command(target, count=1), 30)
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
            commands.extend(
                VerifyCommand(hosts[0].host_name, mesh_ping_command(host.address), 30)
                for host in hosts[1:]
            )
    if full and bgp_plan:
        commands.extend(
            VerifyCommand(observer, command, 20)
            for observer, command in _bgp_data_plane_probes(bgp_plan)
        )
    return collect_verify_commands(runtime, commands)


def _services_ok(runtime, routers: Mapping[str, SonicRouter]) -> bool:
    def running(name: str, router: SonicRouter) -> bool:
        states = {
            fields[0]: fields[1]
            for fields in (
                line.split()
                for line in runtime.exec(name, _services_command(router)).splitlines()
            )
            if len(fields) >= 2
        }
        return all(
            states.get(service) == "RUNNING"
            for service in (*_SERVICES, *router.daemons)
        )

    return all(running(name, router) for name, router in routers.items())


def _addresses_ok(runtime, routers: Mapping[str, SonicRouter]) -> bool:
    """CONFIG_DB interface addresses are present in each router's kernel."""
    for name, router in routers.items():
        try:
            present = {
                (row["ifname"], f"{entry['local']}/{entry['prefixlen']}")
                for row in runtime.json(name, _ADDRESSES)
                for entry in row.get("addr_info", [])
            }
        except (ValueError, KeyError, TypeError):
            return False
        if any(
            (iface, address) not in present
            for iface, addresses in router.addresses.items()
            for address in addresses
            if ":" not in address
        ):
            return False
    return True


def _appl_routes_ok(runtime, plan: IspPlan, bgp_plan: BgpPlan | None) -> bool:
    """FRR's IGP and BGP routes reach SONiC's APPL_DB through FPM."""
    loopbacks = {node.device_name: node.loopback for node in plan.nodes}
    wanted: dict[str, set[str]] = {node.device_name: set() for node in plan.nodes}
    for component in igp_components(plan):
        for device in component:
            wanted[device].update(
                f"{loopbacks[peer]}/32" for peer in component if peer != device
            )
    for observer, prefix in bgp_plan.expect_reachable if bgp_plan else ():
        wanted[observer].add(prefix)
    for device, prefixes in wanted.items():
        # APPL_DB keys host routes by address alone.
        keys = {
            str(ip_network(line.removeprefix("ROUTE_TABLE:")))
            for line in runtime.exec(device, _APPL_ROUTES).splitlines()
            if line.startswith("ROUTE_TABLE:")
        }
        if not prefixes <= keys:
            return False
    return True


def _bgp_data_plane_probes(bgp_plan: BgpPlan) -> list[tuple[str, str]]:
    """Observer pings to the address inside each prefix it must reach.

    Export policy keeps infrastructure addresses out of a peer AS, so an
    observer peering with the origin sources its ping from the eBGP link.
    """
    origins = {origin.prefix: origin for origin in bgp_plan.originated}
    probes = []
    for observer, prefix in bgp_plan.expect_reachable:
        origin = origins.get(prefix)
        if origin is None:
            continue
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
        command = ping_command(origin.ping_address, count=3)
        if source:
            command = command.replace("ping ", f"ping -I {source} ", 1)
        probes.append((observer, command))
    return probes


def _bgp_data_plane_ok(runtime, bgp_plan: BgpPlan) -> bool:
    return all(
        "3 received" in runtime.exec(observer, command)
        for observer, command in _bgp_data_plane_probes(bgp_plan)
    )


def verify_isp_sonic_lab(
    runtime: LabRuntime,
    *,
    plan: IspPlan,
    routers: Mapping[str, SonicRouter],
    port_name: Callable[[str], str],
    scenario_name: str,
    bgp_plan: BgpPlan | None = None,
    traffic: IspTrafficAttachment | None = None,
    full: bool = True,
) -> dict[str, Any]:
    """Verify a SONiC ISP lab; ``full=False`` is the bounded startup check."""
    expected = [node.device_name for node in plan.nodes]
    if traffic is not None:
        expected.extend(h.host_name for h in traffic.hosts)
    all_hosts = [host.host_name for host in traffic.hosts] if traffic else []
    traffic = sample_traffic(traffic, bgp_plan=bgp_plan)
    runtime = _collect(runtime, plan, bgp_plan, traffic, routers, full=full)
    checks: dict[str, bool] = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "sonic_services": _services_ok(runtime, routers),
        "igp_adjacencies": frr._igp_adjacencies_ok(runtime, plan),
        "loopbacks_reachable": clab._loopbacks_reachable(runtime, plan, traffic, None),
    }
    details: dict[str, Any] = {
        "topology_name": plan.topology_name,
        "igp": plan.igp,
        "bgp_mode": bgp_plan.mode if bgp_plan is not None else "none",
        "device_profile": "sonic",
    }
    if bgp_plan is not None:
        checks["bgp_sessions"] = frr._bgp_sessions_ok(runtime, bgp_plan)
        checks["bgp_prefixes_propagated"] = frr._bgp_prefixes_propagated_ok(
            runtime, bgp_plan
        )
    if not full:
        return runtime.finish(
            build_lab_verify_result(
                scenario_name=scenario_name,
                verified=all(checks.values()),
                checks=checks,
                details=details,
            )
        )
    checks["inventory_addresses"] = _addresses_ok(runtime, routers)
    checks["appl_db_routes"] = _appl_routes_ok(runtime, plan, bgp_plan)
    details.update(
        node_count=len(plan.nodes),
        link_count=len(plan.links),
        inventory=plan.inventory,
    )
    if traffic is not None:
        checks["stub_hosts_addressed"] = clab._stub_hosts_addressed_ok(runtime, traffic)
        checks["stub_gateway_reachable"] = clab._stub_gateway_ok(runtime, traffic)
        checks["stub_remote_reachable"] = clab._stub_remote_ok(runtime, traffic)
        details["hosts"] = plan.inventory.get("hosts")
    if bgp_plan is not None:
        checks["bgp_prefixes_originated"] = frr._bgp_prefixes_originated_ok(
            runtime, bgp_plan
        )
        checks["bgp_infra_denied"] = frr._bgp_infra_denied_ok(runtime, bgp_plan)
        checks["bgp_data_plane"] = _bgp_data_plane_ok(runtime, bgp_plan)
        details["bgp"] = bgp_plan.inventory
    fibs, fib_errors = {}, {}
    for node in plan.nodes:
        try:
            fibs[node.device_name] = linux_fib(
                runtime.json(node.device_name, LINUX_FIB_COMMAND)
            )
        except (ValueError, KeyError, TypeError) as exc:
            fib_errors[node.device_name] = str(exc)
    fib_report = verify_fib(plan, fibs, errors=fib_errors, port_name=port_name)
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
