"""Startup verification for Containerlab ISP (Nokia SR Linux)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from nika.net_env.isp.bgp.plan import BgpPlan
from nika.net_env.isp.igp.plan import IspPlan, active_igp_links, igp_components
from nika.net_env.isp.traffic.stubs import IspTrafficAttachment
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime


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
        checks["bgp_prefixes_propagated"] = _bgp_prefixes_propagated_ok(
            runtime, bgp_plan
        )
        details["bgp"] = bgp_plan.inventory
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details=details,
    )


def _igp_adjacencies_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    if not plan.links:
        return True
    if plan.igp == "isis":
        return _isis_adjacencies_ok(runtime, plan)
    if plan.igp == "ospf":
        return _ospf_adjacencies_ok(runtime, plan)
    return False


def _isis_adjacencies_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    degree: dict[str, int] = defaultdict(int)
    for link in active_igp_links(plan):
        degree[link.endpoint_a] += 1
        degree[link.endpoint_b] += 1
    for node in plan.nodes:
        need = degree[node.device_name]
        if need == 0:
            continue
        output = exec_or_empty(
            runtime,
            node.device_name,
            'sr_cli "show network-instance default protocols isis adjacency"',
            timeout=30,
        )
        up = sum(1 for line in output.splitlines() if "up" in line.lower())
        if up < need:
            return False
    return True


def _ospf_adjacencies_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    degree: dict[str, int] = defaultdict(int)
    for link in active_igp_links(plan):
        degree[link.endpoint_a] += 1
        degree[link.endpoint_b] += 1
    for node in plan.nodes:
        need = degree[node.device_name]
        if need == 0:
            continue
        output = exec_or_empty(
            runtime,
            node.device_name,
            'sr_cli "show network-instance default protocols ospf neighbor"',
            timeout=30,
        )
        up = sum(
            1
            for line in output.splitlines()
            if "full" in line.lower() or "2way" in line.lower()
        )
        if up < need:
            return False
    return True


def _loopbacks_reachable(
    runtime: LabRuntime,
    plan: IspPlan,
    traffic: IspTrafficAttachment | None,
) -> bool:
    components = igp_components(plan)
    if traffic is None or not traffic.hosts:
        loopback = {node.device_name: node.loopback for node in plan.nodes}
        for component in components:
            if len(component) >= 2:
                return ping_ok(runtime, component[0], loopback[component[-1]], count=1)
        return True
    host_by_router = {host.router_device: host for host in traffic.hosts}
    loopback = {node.device_name: node.loopback for node in plan.nodes}
    for component in components:
        routers = [router for router in component if router in host_by_router]
        if len(routers) >= 2:
            return ping_ok(
                runtime,
                host_by_router[routers[0]].host_name,
                loopback[routers[-1]],
                count=1,
            )
    return True


def _inventory_addresses_ok(runtime: LabRuntime, plan: IspPlan) -> bool:
    # Confirm loopback present via sr_cli interface summary.
    for node in plan.nodes:
        output = exec_or_empty(
            runtime,
            node.device_name,
            'sr_cli "show interface system0"',
            timeout=20,
        )
        if node.loopback not in output:
            return False
    return True


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
    if not traffic.hosts:
        return True
    for host in traffic.hosts:
        if not ping_ok(runtime, host.host_name, host.gateway, count=1):
            return False
    return True


def _stub_remote_ok(runtime: LabRuntime, traffic: IspTrafficAttachment) -> bool:
    host_by_router = {host.router_device: host for host in traffic.hosts}
    for component in igp_components(traffic.plan):
        hosts = sorted(
            (
                host_by_router[router]
                for router in component
                if router in host_by_router
            ),
            key=lambda host: host.host_name,
        )
        if len(hosts) >= 2:
            return ping_ok(runtime, hosts[0].host_name, hosts[-1].address, count=1)
    return True


def _bgp_sessions_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    needed: dict[str, set[str]] = defaultdict(set)
    for sess in bgp_plan.sessions:
        needed[sess.local_device].add(sess.remote_ip)
    for device, peers in needed.items():
        output = exec_or_empty(
            runtime,
            device,
            'sr_cli "show network-instance default protocols bgp neighbor"',
            timeout=30,
        )
        established = sum(
            1 for line in output.splitlines() if "established" in line.lower()
        )
        if established < len(peers):
            return False
    return True


def _bgp_prefixes_originated_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    for pref in bgp_plan.originated:
        output = exec_or_empty(
            runtime,
            pref.device,
            'sr_cli "show network-instance default protocols bgp routes ipv4 summary"',
            timeout=30,
        )
        network = pref.prefix.split("/")[0]
        if network not in output and pref.prefix not in output:
            # Business prefixes live on lo0.* (ixr-d2l system0 is single-address).
            iface_out = exec_or_empty(
                runtime,
                pref.device,
                'sr_cli "show interface lo0"',
                timeout=20,
            )
            if pref.ping_address not in iface_out:
                system_out = exec_or_empty(
                    runtime,
                    pref.device,
                    'sr_cli "show interface system0"',
                    timeout=20,
                )
                if pref.ping_address not in system_out:
                    return False
    return True


def _srl_ping_ok(runtime: LabRuntime, device: str, target: str) -> bool:
    """Ping from an SRL router via sr_cli (linux ping has no data-plane netns)."""
    output = exec_or_empty(
        runtime,
        device,
        f'sr_cli "ping {target} network-instance default -c 1"',
        timeout=20,
    )
    return "1 received" in output or "1 packets received" in output


def _bgp_prefixes_propagated_ok(runtime: LabRuntime, bgp_plan: BgpPlan) -> bool:
    ping_by_prefix = {o.prefix: o.ping_address for o in bgp_plan.originated}
    for observer, prefix in bgp_plan.expect_reachable:
        target = ping_by_prefix.get(prefix)
        if target and not _srl_ping_ok(runtime, observer, target):
            return False
    return True
