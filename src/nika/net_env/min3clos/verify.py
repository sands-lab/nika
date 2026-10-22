"""Startup verification signals for the min3clos Containerlab scenario."""

from __future__ import annotations

from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    link_up,
    nodes_deployed,
    ping_unreachable,
    srl_bgp_established_peers,
)
from nika.runtime.base import LabRuntime
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    mesh_ping_command,
)

CLIENT1 = "client1"
CLIENT2 = "client2"
LEAF1 = "leaf1"

CLIENT1_IP = "10.0.0.25"
CLIENT2_IP = "10.0.0.27"
CLIENT1_GATEWAY = "10.0.0.24"
CLIENT2_GATEWAY = "10.0.0.26"
CLIENT_INTF = "eth1"
CLIENT1_IPV6 = "1000:10::25"
CLIENT2_IPV6 = "1000:10::27"
CLIENT1_GATEWAY_IPV6 = "1000:10::24"
CLIENT2_GATEWAY_IPV6 = "1000:10::26"

# system0.0 loopbacks (configs/*.yaml), advertised by IS-IS and OSPF.
ROUTER_LOOPBACKS = (
    "30.0.0.1",
    "30.0.0.2",
    "30.0.0.3",
    "3000:30::1",
    "3000:30::2",
    "3000:30::3",
)

EXPECTED_NODES = (
    "leaf1",
    "leaf2",
    "spine",
    "client1",
    "client2",
)

# Planned eBGP sessions (configs/*.yaml): each leaf peers with the spine.
BGP_PEERS = {
    "leaf1": frozenset({"10.0.0.1"}),
    "leaf2": frozenset({"10.0.0.5"}),
    "spine": frozenset({"10.0.0.0", "10.0.0.4"}),
}

# client -> (address, gateway, peer client address)
_CLIENTS = {
    CLIENT1: (CLIENT1_IP, CLIENT1_GATEWAY, CLIENT2_IP),
    CLIENT2: (CLIENT2_IP, CLIENT2_GATEWAY, CLIENT1_IP),
}
_CLIENTS_IPV6 = {
    CLIENT1: (CLIENT1_IPV6, CLIENT1_GATEWAY_IPV6, CLIENT2_IPV6),
    CLIENT2: (CLIENT2_IPV6, CLIENT2_GATEWAY_IPV6, CLIENT1_IPV6),
}


def _bgp_sessions_established(runtime: LabRuntime) -> bool:
    """Every planned eBGP session is Established on both ends."""
    for router, peers in BGP_PEERS.items():
        output = exec_or_empty(
            runtime,
            router,
            'sr_cli "show network-instance default protocols bgp neighbor"',
            timeout=30,
        )
        if not peers <= srl_bgp_established_peers(output):
            return False
    return True


def _clients_addressed(runtime: LabRuntime) -> bool:
    return all(
        link_up(runtime, client, CLIENT_INTF)
        and host_has_ipv4(runtime, client, address, intf=CLIENT_INTF)
        for client, (address, _, _) in _CLIENTS.items()
    )


def _collect(runtime, *, full):
    commands = [
        VerifyCommand(
            router, 'sr_cli "show network-instance default protocols bgp neighbor"', 30
        )
        for router in BGP_PEERS
    ]
    for client, (_, gateway, peer) in _CLIENTS.items():
        commands.extend(
            [
                VerifyCommand(
                    client, f"cat /sys/class/net/{CLIENT_INTF}/operstate", 10
                ),
                VerifyCommand(client, f"ip -4 -o addr show dev {CLIENT_INTF}", 10),
            ]
        )
        if full:
            _, gateway6, peer6 = _CLIENTS_IPV6[client]
            commands.extend(
                VerifyCommand(client, mesh_ping_command(target), 30)
                for target in (gateway, peer, gateway6, peer6, *ROUTER_LOOPBACKS)
            )
    return collect_verify_commands(runtime, commands)


def verify_min3clos_lab_startup(runtime: LabRuntime, *, scenario_name: str) -> dict:
    """Bounded readiness: fabric nodes, client attachment, and all BGP sessions."""
    runtime = _collect(runtime, full=False)
    checks = {
        "nodes_deployed": nodes_deployed(runtime, EXPECTED_NODES),
        "clients_addressed": _clients_addressed(runtime),
        "bgp_sessions_established": _bgp_sessions_established(runtime),
    }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={"clients": sorted(_CLIENTS), "bgp_peers": sorted(BGP_PEERS)},
        )
    )


def verify_min3clos_lab(runtime: LabRuntime, *, scenario_name: str) -> dict:
    """Check provisioning, all BGP sessions, and dual-stack reachability.

    Clients reach their gateway and the other client over IPv4 and IPv6, and
    every router loopback (system0) over both families.
    """
    runtime = _collect(runtime, full=True)
    clients_ready = _clients_addressed(runtime)
    checks = {
        "nodes_deployed": nodes_deployed(runtime, EXPECTED_NODES),
        "clients_addressed": clients_ready,
        "bgp_sessions_established": _bgp_sessions_established(runtime),
        "client_gateways_reachable": clients_ready
        and not any(
            ping_unreachable(runtime, client, [gateway])
            for client, (_, gateway, _) in _CLIENTS.items()
        ),
        "cross_leaf_clients_reachable": clients_ready
        and not any(
            ping_unreachable(runtime, client, [peer])
            for client, (_, _, peer) in _CLIENTS.items()
        ),
        "client_ipv6_gateways_reachable": clients_ready
        and not any(
            ping_unreachable(runtime, client, [gateway])
            for client, (_, gateway, _) in _CLIENTS_IPV6.items()
        ),
        "cross_leaf_clients_reachable_ipv6": clients_ready
        and not any(
            ping_unreachable(runtime, client, [peer])
            for client, (_, _, peer) in _CLIENTS_IPV6.items()
        ),
        "router_loopbacks_reachable": clients_ready
        and not any(
            ping_unreachable(runtime, client, ROUTER_LOOPBACKS) for client in _CLIENTS
        ),
    }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details={"clients": sorted(_CLIENTS), "bgp_peers": sorted(BGP_PEERS)},
        )
    )
