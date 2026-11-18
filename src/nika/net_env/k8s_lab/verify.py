"""Startup verification signals for the k8s_lab scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.utils.k8s_client_hosts import (
    sync_k8s_client_hosts,
    weather_app_http_url,
    word_app_http_url,
)
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    frr_bgp_established_peers,
    host_has_ipv4,
    http_ok,
    k8s_namespace_phase_active,
    k8s_ready_node_count,
    nodes_deployed,
    ping_ok,
    ping_unreachable,
)
from nika.runtime.base import LabRuntime
from nika.net_env.verify_collection import (
    VerifyCommand,
    collect_verify_commands,
    ping_command,
    mesh_ping_command,
    http_command,
)
from nika.utils.parallel import bounded_parallel_map

_K8S_EXPECTED = (
    "controller",
    "worker1",
    "worker2",
    "worker3",
    "worker4",
    "worker5",
    "client",
)

# Planned BGP neighbors from each router's bgpd.conf: unnumbered fabric
# interfaces (FRR shows them as ``<hostname>(ethN)``) and the AS1-AS2 link.
# MetalLB speakers peer dynamically with leaf_1_x and are covered by the
# ingress HTTP checks.
_FABRIC_BGP_PEERS = {
    **{
        router: ("eth0", "eth1")
        for router in ("leaf_1_1", "leaf_1_2", "leaf_2_1", "leaf_2_2")
    },
    **{
        router: ("eth0", "eth1", "eth2", "eth3")
        for router in (
            "spine_1_1",
            "spine_1_2",
            "spine_2_1",
            "spine_2_2",
            "core_1_1",
            "core_1_2",
        )
    },
    "as1r1": ("10.0.4.2",),
    "as2r1": ("10.0.4.1",),
}

# k3s node addresses on the leaf access networks.
_K3S_NODE_ADDRESSES = {
    "controller": "201.1.1.2",
    "worker1": "201.1.2.2",
    "worker2": "201.1.3.2",
    "worker3": "201.2.1.2",
    "worker4": "201.2.2.2",
    "worker5": "201.2.3.2",
}


def _bgp_peer_matches(planned: str, established: set[str]) -> bool:
    return any(peer == planned or peer.endswith(f"({planned})") for peer in established)


def _fabric_bgp_failures(runtime: LabRuntime) -> dict[str, list[str]]:
    """Return planned BGP neighbors that are not Established, by router."""

    def missing(item: tuple[str, tuple[str, ...]]) -> list[str]:
        router, peers = item
        summary = exec_or_empty(
            runtime, router, "vtysh -c 'show bgp summary'", timeout=20
        )
        established = frr_bgp_established_peers(summary)
        return [peer for peer in peers if not _bgp_peer_matches(peer, established)]

    items = sorted(_FABRIC_BGP_PEERS.items())
    return {
        router: peers
        for (router, _), peers in zip(
            items, bounded_parallel_map(missing, items), strict=True
        )
        if peers
    }


def _verify_k8s_lab(
    runtime: LabRuntime, *, scenario_name: str, full: bool
) -> dict[str, Any]:
    """Fast readiness for inject: nodes up plus sample app namespaces/HTTP.

    Workload-scoped faults (e.g. NetworkPolicy deny on ``word-ns``) inject
    immediately after ``start_net_env`` returns, so startup must wait until
    controller.startup has applied sample namespaces and both sample ingress
    paths answer — not only until k3s nodes report Ready.
    """
    live_runtime = runtime
    commands = [
        VerifyCommand("controller", command, 10 if command.startswith("ip ") else 60)
        for command in (
            "kubectl get nodes --no-headers",
            "kubectl get ns word-ns -o jsonpath={.status.phase}",
            "kubectl get ns weather-ns -o jsonpath={.status.phase}",
            "kubectl get svc -n ingress-nginx ingress-nginx-controller -o jsonpath={.status.loadBalancer.ingress[0].ip}",
            "ip -4 -o addr show dev eth0",
        )
    ]
    if full:
        commands.extend(
            VerifyCommand(router, "vtysh -c 'show bgp summary'")
            for router in _FABRIC_BGP_PEERS
        )
    runtime = collect_verify_commands(live_runtime, commands)
    nodes = exec_or_empty(
        runtime, "controller", "kubectl get nodes --no-headers", timeout=60
    )
    ready_nodes = k8s_ready_node_count(nodes)
    word_ns = exec_or_empty(
        runtime,
        "controller",
        "kubectl get ns word-ns -o jsonpath={.status.phase}",
        timeout=60,
    )
    weather_ns = exec_or_empty(
        runtime,
        "controller",
        "kubectl get ns weather-ns -o jsonpath={.status.phase}",
        timeout=60,
    )
    ingress_ip = exec_or_empty(
        runtime,
        "controller",
        "kubectl get svc -n ingress-nginx ingress-nginx-controller "
        "-o jsonpath={.status.loadBalancer.ingress[0].ip}",
        timeout=60,
    ).strip()
    sync_k8s_client_hosts(live_runtime, ingress_ip=ingress_ip)
    word_url = word_app_http_url(ingress_ip)
    weather_url = weather_app_http_url(ingress_ip)
    commands = [
        VerifyCommand("client", ping_command("201.1.1.2", count=3), 15),
        VerifyCommand("client", http_command(word_url)),
        VerifyCommand("client", http_command(weather_url)),
    ]
    if full:
        commands.extend(
            VerifyCommand("client", mesh_ping_command(address), 30)
            for address in _K3S_NODE_ADDRESSES.values()
        )
    runtime = runtime.merged(
        collect_verify_commands(live_runtime, commands, nodes=runtime.nodes)
    )
    checks = {
        "nodes_deployed": nodes_deployed(runtime, _K8S_EXPECTED),
        "controller_ipv4": host_has_ipv4(runtime, "controller", "201.1.1.2"),
        "client_reaches_controller": ping_ok(runtime, "client", "201.1.1.2", count=3),
        "k3s_nodes_ready": ready_nodes >= 6,
        "sample_namespaces": k8s_namespace_phase_active(word_ns)
        and k8s_namespace_phase_active(weather_ns),
        "ingress_vip_allocated": ingress_ip.startswith("101."),
        "word_app_http": http_ok(runtime, "client", word_url),
        "weather_app_http": http_ok(runtime, "client", weather_url),
    }
    details = {
        "ready_nodes": ready_nodes,
        "ingress_ip": ingress_ip,
        "word_url": word_url,
        "weather_url": weather_url,
    }
    if full:
        bgp_failures = _fabric_bgp_failures(runtime)
        unreachable = ping_unreachable(runtime, "client", _K3S_NODE_ADDRESSES.values())
        checks.update(
            fabric_bgp_established=not bgp_failures,
            client_reaches_k3s_nodes=not unreachable,
        )
        details["failures"] = {
            "bgp_not_established": bgp_failures,
            "unreachable_k3s_nodes": unreachable,
        }
    return runtime.finish(
        build_lab_verify_result(
            scenario_name=scenario_name,
            verified=all(checks.values()),
            checks=checks,
            details=details,
        )
    )


def verify_k8s_lab_startup(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    return _verify_k8s_lab(runtime, scenario_name=scenario_name, full=False)


def verify_k8s_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    return _verify_k8s_lab(runtime, scenario_name=scenario_name, full=True)
