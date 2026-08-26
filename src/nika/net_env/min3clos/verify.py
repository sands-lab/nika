"""Startup verification signals for the min3clos Containerlab scenario."""

from __future__ import annotations

from nika.net_env.verify import build_lab_verify_result
from nika.runtime.base import LabRuntime

CLIENT1 = "client1"
CLIENT2 = "client2"
LEAF1 = "leaf1"

CLIENT1_IP = "10.0.0.25"
CLIENT2_IP = "10.0.0.27"
CLIENT1_GATEWAY = "10.0.0.24"

EXPECTED_NODES = (
    "leaf1",
    "leaf2",
    "spine",
    "client1",
    "client2",
)

MIN_LEAF_BGP_NEIGHBORS = 1


def _ping_ok(runtime: LabRuntime, host: str, target: str) -> bool:
    output = runtime.exec(host, f"ping -c 1 -W 2 {target}", timeout=10)
    return "1 received" in output


def _link_up(runtime: LabRuntime, host: str, intf: str) -> bool:
    output = runtime.exec(host, f"cat /sys/class/net/{intf}/operstate", timeout=10)
    return output.strip() == "up"


def _host_has_ipv4(runtime: LabRuntime, host: str, intf: str, address: str) -> bool:
    output = runtime.exec(host, f"ip -4 -o addr show dev {intf}", timeout=10)
    return address in output


def _nodes_deployed(runtime: LabRuntime) -> bool:
    deployed = set(runtime.list_nodes())
    return all(node in deployed for node in EXPECTED_NODES)


def _leaf_bgp_neighbors_established(
    runtime: LabRuntime, leaf: str, *, min_neighbors: int
) -> bool:
    output = runtime.exec(
        leaf,
        'sr_cli "show network-instance default protocols bgp neighbor"',
        timeout=30,
    )
    established = sum(
        1 for line in output.splitlines() if "established" in line.lower()
    )
    return established >= min_neighbors


def verify_min3clos_lab(runtime: LabRuntime, *, scenario_name: str) -> dict:
    """Check fabric provisioning, BGP convergence, and end-to-end client reachability."""
    client_ready = _link_up(runtime, CLIENT1, "eth1") and _host_has_ipv4(
        runtime, CLIENT1, "eth1", CLIENT1_IP
    )

    checks = {
        "nodes_deployed": _nodes_deployed(runtime),
        "client1_link_up": _link_up(runtime, CLIENT1, "eth1"),
        "client1_ipv4": _host_has_ipv4(runtime, CLIENT1, "eth1", CLIENT1_IP),
        "leaf1_bgp_neighbors": _leaf_bgp_neighbors_established(
            runtime, LEAF1, min_neighbors=MIN_LEAF_BGP_NEIGHBORS
        ),
        "client_gateway_reachable": _ping_ok(runtime, CLIENT1, CLIENT1_GATEWAY)
        if client_ready
        else False,
        "cross_leaf_client_reachable": _ping_ok(runtime, CLIENT1, CLIENT2_IP)
        if client_ready
        else False,
    }

    verified = all(checks.values())
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=verified,
        checks=checks,
        details={
            "probe_client": CLIENT1,
            "peer_client": CLIENT2,
            "probe_leaf": LEAF1,
        },
    )
