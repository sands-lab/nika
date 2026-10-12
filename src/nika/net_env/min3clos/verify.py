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

CLIENT1 = "client1"
CLIENT2 = "client2"
LEAF1 = "leaf1"

CLIENT1_IP = "10.0.0.25"
CLIENT2_IP = "10.0.0.27"
CLIENT1_GATEWAY = "10.0.0.24"
CLIENT2_GATEWAY = "10.0.0.26"
CLIENT_INTF = "eth1"

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


def verify_min3clos_lab_startup(runtime: LabRuntime, *, scenario_name: str) -> dict:
    """Bounded readiness: fabric nodes, client attachment, and all BGP sessions."""
    checks = {
        "nodes_deployed": nodes_deployed(runtime, EXPECTED_NODES),
        "clients_addressed": _clients_addressed(runtime),
        "bgp_sessions_established": _bgp_sessions_established(runtime),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={"clients": sorted(_CLIENTS), "bgp_peers": sorted(BGP_PEERS)},
    )


def verify_min3clos_lab(runtime: LabRuntime, *, scenario_name: str) -> dict:
    """Check provisioning, all BGP sessions, and client reachability both ways."""
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
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={"clients": sorted(_CLIENTS), "bgp_peers": sorted(BGP_PEERS)},
    )
