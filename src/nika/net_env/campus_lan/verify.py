"""Startup verification signals for campus_lan scenarios."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from nika.net_env.verify import build_lab_verify_result
from nika.runtime.base import LabRuntime
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    ping_command,
    http_command,
)

PROBE_HOST = "pc_1_1_1_1"
PEER_HOST = "pc_2_1_1_1"
CORE_ROUTER = "router_core_1"
DIST_ROUTER_STATIC = "switch_dist_1_1"
DIST_ROUTER_DHCP = "router_dist_1_1"
DNS_SERVER = "dns_server"
WEB_SERVER = "web_server_0"
LOAD_BALANCER = "load_balancer"

HOST_GATEWAY = "10.1.1.1"
HOST_STATIC_IP = "10.1.1.2"
HOST_PEER_STATIC_IP = "10.2.1.2"
DNS_SERVER_IP = "10.200.0.2"
WEB0_IP = "10.200.0.3"
WEB0_URL = "http://web0.local/"
WEB3_URL = "http://web3.local/"
WEB99_URL = "http://web99.local/"

MIN_OSPF_NEIGHBORS = 2
DHCP_HOST_RANGE = re.compile(r"10\.1\.1\.(?:[1-9]\d|100)")


@dataclass(frozen=True)
class CampusRouterQueries:
    """Router commands and readers for one device profile."""

    ospf_process: str
    ospf_running: Callable[[str], bool]
    ospf_neighbors: str
    ospf_full_count: Callable[[str], int]
    gateway_addresses: str
    dhcp_relay: str
    dhcp_relay_running: Callable[[str], bool]
    # First data interface of the core router (container interface name).
    core_interface: str = "eth0"


FRR_QUERIES = CampusRouterQueries(
    ospf_process="vtysh -c 'show ip ospf'",
    ospf_running=lambda output: "Routing Process" in output,
    ospf_neighbors="vtysh -c 'show ip ospf neighbor'",
    ospf_full_count=lambda output: sum(
        1 for line in output.splitlines() if "Full" in line
    ),
    gateway_addresses="ip -4 -o addr show dev br0",
    dhcp_relay="pgrep -a dhcrelay",
    dhcp_relay_running=lambda output: "dhcrelay" in output,
)


def _ping_ok(runtime: LabRuntime, host: str, target: str) -> bool:
    output = runtime.exec(host, ping_command(target), timeout=10)
    return "1 received" in output


def _service_active(runtime: LabRuntime, host: str, unit: str) -> bool:
    output = runtime.exec(host, f"systemctl is-active {unit}", timeout=10)
    return output.strip() == "active"


def _process_running(runtime: LabRuntime, host: str, process: str) -> bool:
    output = runtime.exec(host, f"pgrep -x {process}", timeout=10)
    return bool(output.strip())


def _http_status(runtime: LabRuntime, host: str, url: str) -> str:
    return runtime.exec(
        host,
        http_command(url),
        timeout=15,
    ).strip()


def _http_ok(runtime: LabRuntime, host: str, url: str) -> bool:
    return _http_status(runtime, host, url) == "200"


def _dns_resolves(
    runtime: LabRuntime, host: str, name: str, *, expect_ip: str | None = None
) -> bool:
    output = runtime.exec(host, f"getent hosts {name}", timeout=10)
    if name not in output:
        return False
    if expect_ip is not None and expect_ip not in output:
        return False
    return True


def _default_route_via(runtime: LabRuntime, host: str, gateway: str) -> bool:
    output = runtime.exec(host, "ip route show default", timeout=10)
    return f"via {gateway}" in output


def _link_up(runtime: LabRuntime, host: str, intf: str = "eth0") -> bool:
    output = runtime.exec(host, f"cat /sys/class/net/{intf}/operstate", timeout=10)
    return output.strip() == "up"


def _ospf_neighbors_full(
    runtime: LabRuntime,
    queries: CampusRouterQueries,
    *,
    min_neighbors: int = MIN_OSPF_NEIGHBORS,
) -> bool:
    output = runtime.exec(CORE_ROUTER, queries.ospf_neighbors, timeout=15)
    return queries.ospf_full_count(output) >= min_neighbors


def _ospf_process_running(runtime: LabRuntime, queries: CampusRouterQueries) -> bool:
    output = runtime.exec(CORE_ROUTER, queries.ospf_process, timeout=15)
    return queries.ospf_running(output)


def _dist_gateway_configured(
    runtime: LabRuntime, dist_router: str, queries: CampusRouterQueries
) -> bool:
    output = runtime.exec(dist_router, queries.gateway_addresses, timeout=10)
    return HOST_GATEWAY in output


def _host_has_ipv4(runtime: LabRuntime, host: str = PROBE_HOST) -> bool:
    output = runtime.exec(host, "ip -4 -o addr show dev eth0", timeout=10)
    return "inet " in output


def _host_static_ip_ok(runtime: LabRuntime, host: str = PROBE_HOST) -> bool:
    output = runtime.exec(host, "ip -4 -o addr show dev eth0", timeout=10)
    return HOST_STATIC_IP in output


def _host_dhcp_ip_ok(runtime: LabRuntime, host: str = PROBE_HOST) -> bool:
    output = runtime.exec(host, "ip -4 -o addr show dev eth0", timeout=10)
    return bool(DHCP_HOST_RANGE.search(output))


def _peer_host_ip(runtime: LabRuntime, host: str = PEER_HOST) -> str | None:
    output = runtime.exec(host, "ip -4 -o addr show dev eth0", timeout=10)
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", output)
    if not match:
        return None
    return match.group(1)


def _peer_host_reachable(
    runtime: LabRuntime, *, static_peer_ip: str | None = None
) -> bool:
    peer_ip = static_peer_ip or _peer_host_ip(runtime)
    if not peer_ip:
        return False
    return _ping_ok(runtime, PROBE_HOST, peer_ip)


def _shared_checks(
    runtime: LabRuntime, dist_router: str, queries: CampusRouterQueries
) -> dict[str, bool]:
    host_ready = _host_has_ipv4(runtime)
    checks = {
        "ospf_process": _ospf_process_running(runtime, queries),
        "ospf_neighbors": _ospf_neighbors_full(runtime, queries),
        "core_link_up": _link_up(runtime, CORE_ROUTER, queries.core_interface),
        "dist_gateway_configured": _dist_gateway_configured(
            runtime, dist_router, queries
        ),
        "host_ipv4": host_ready,
        "host_default_route": _default_route_via(runtime, PROBE_HOST, HOST_GATEWAY)
        if host_ready
        else False,
        "gateway_reachable": _ping_ok(runtime, PROBE_HOST, HOST_GATEWAY)
        if host_ready
        else False,
        "peer_host_reachable": _peer_host_reachable(runtime) if host_ready else False,
        "dns_server_reachable": _ping_ok(runtime, PROBE_HOST, DNS_SERVER_IP)
        if host_ready
        else False,
        # systemctl.py reports notify/simple units inactive; check the process.
        "dns_service_active": _process_running(runtime, DNS_SERVER, "named"),
        "dns_resolution_web0": _dns_resolves(
            runtime, PROBE_HOST, "web0.local", expect_ip=WEB0_IP
        )
        if host_ready
        else False,
        "web_http_web0": _http_ok(runtime, PROBE_HOST, WEB0_URL)
        if host_ready
        else False,
        "cross_pod_web_http": _http_ok(runtime, PEER_HOST, WEB3_URL)
        if _host_has_ipv4(runtime, PEER_HOST)
        else False,
    }
    return checks


def _collect(runtime, queries: CampusRouterQueries, *, full):
    live_runtime = runtime
    commands = [
        VerifyCommand(node, command, timeout)
        for node, command, timeout in (
            (PROBE_HOST, "ip -4 -o addr show dev eth0", 10),
            (PEER_HOST, "ip -4 -o addr show dev eth0", 10),
            (PROBE_HOST, "ip route show default", 10),
            (CORE_ROUTER, queries.ospf_process, 15),
            (CORE_ROUTER, queries.ospf_neighbors, 15),
            (CORE_ROUTER, f"cat /sys/class/net/{queries.core_interface}/operstate", 10),
            (DIST_ROUTER_DHCP, queries.gateway_addresses, 10),
        )
    ]
    if full:
        commands.extend(
            VerifyCommand(node, command, 10)
            for node, command in (
                (DNS_SERVER, "pgrep -x named"),
                ("dhcp_server", "systemctl is-active isc-dhcp-server"),
                (DIST_ROUTER_DHCP, queries.dhcp_relay),
                (WEB_SERVER, "pgrep -f '[w]eb_server.py'"),
                (LOAD_BALANCER, "pgrep -x nginx"),
            )
        )
    collected = collect_verify_commands(live_runtime, commands)
    peer_ip = _peer_host_ip(collected)
    commands = [VerifyCommand(PROBE_HOST, ping_command(HOST_GATEWAY), 10)]
    if peer_ip:
        commands.append(VerifyCommand(PROBE_HOST, ping_command(peer_ip), 10))
    if full:
        commands.extend(
            [
                VerifyCommand(PROBE_HOST, ping_command(DNS_SERVER_IP), 10),
                VerifyCommand(PROBE_HOST, http_command(WEB0_URL), 15),
                VerifyCommand(PROBE_HOST, http_command(WEB99_URL), 15),
                VerifyCommand(PEER_HOST, http_command(WEB3_URL), 15),
            ]
        )
        commands.extend(
            VerifyCommand(PROBE_HOST, f"getent hosts {name}", 10)
            for name in ("web0.local", "web99.local")
        )
    return collected.merged(
        collect_verify_commands(live_runtime, commands, nodes=collected.nodes)
    )


def verify_campus_lan_lab_startup(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    queries: CampusRouterQueries = FRR_QUERIES,
) -> dict:
    """Bounded readiness: core OSPF and the probe-host to peer-host path."""
    runtime = _collect(runtime, queries, full=False)
    host_ready = _host_has_ipv4(runtime)
    checks = {
        "ospf_process": _ospf_process_running(runtime, queries),
        "ospf_neighbors": _ospf_neighbors_full(runtime, queries),
        "core_link_up": _link_up(runtime, CORE_ROUTER, queries.core_interface),
        "dist_gateway_configured": _dist_gateway_configured(
            runtime, DIST_ROUTER_DHCP, queries
        ),
        "host_ipv4": host_ready,
        "host_default_route": _default_route_via(runtime, PROBE_HOST, HOST_GATEWAY)
        if host_ready
        else False,
        "gateway_reachable": _ping_ok(runtime, PROBE_HOST, HOST_GATEWAY)
        if host_ready
        else False,
        "peer_host_reachable": _peer_host_reachable(runtime) if host_ready else False,
    }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={
                "probe_host": PROBE_HOST,
                "peer_host": PEER_HOST,
                "core_router": CORE_ROUTER,
                "dist_router": DIST_ROUTER_DHCP,
            },
        )
    )


def verify_campus_lan_lab(
    runtime: LabRuntime,
    *,
    scenario_name: str,
    queries: CampusRouterQueries = FRR_QUERIES,
) -> dict:
    """Check routing convergence, L3 connectivity, and service accessibility after deploy."""
    runtime = _collect(runtime, queries, full=True)
    dist_router = DIST_ROUTER_DHCP
    checks = _shared_checks(runtime, dist_router, queries)

    checks["host_dhcp_ip"] = _host_dhcp_ip_ok(runtime)
    checks["dhcp_server_active"] = _service_active(
        runtime, "dhcp_server", "isc-dhcp-server"
    )
    checks["dhcp_relay_active"] = queries.dhcp_relay_running(
        runtime.exec(dist_router, queries.dhcp_relay, timeout=10)
    )
    checks["web_service_active"] = bool(
        runtime.exec(WEB_SERVER, "pgrep -f '[w]eb_server.py'", timeout=10).strip()
    )
    checks["load_balancer_nginx"] = _process_running(runtime, LOAD_BALANCER, "nginx")
    checks["dns_resolution_web99"] = _dns_resolves(runtime, PROBE_HOST, "web99.local")
    checks["load_balancer_http"] = _http_ok(runtime, PROBE_HOST, WEB99_URL)

    verified = all(checks.values())
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=verified,
            checks=checks,
            details={
                "probe_host": PROBE_HOST,
                "peer_host": PEER_HOST,
                "core_router": CORE_ROUTER,
                "dist_router": dist_router,
            },
        )
    )


# Back-compat names used by older callers / tests.
verify_ospf_enterprise_lab = verify_campus_lan_lab
