"""Startup verification signals for the llmd_lab scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.utils.k8s_client_hosts import models_http_url, sync_llmd_client_hosts
from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    http_ok,
    k8s_namespace_phase_active,
    k8s_ready_node_count,
    k8s_unready_pods,
    nodes_deployed,
    ping_ok,
    ping_unreachable,
)
from nika.runtime.base import LabRuntime

_LLMD_EXPECTED = (
    "controller",
    "worker1",
    "worker2",
    "worker3",
    "worker4",
    "worker5",
    "client",
)

# k3s node addresses on the lab LAN.
_K3S_NODE_ADDRESSES = tuple(f"200.0.0.{index}" for index in range(1, 7))

# Namespaces whose pods carry the serving path: MetalLB (gateway VIP), the
# agentgateway data plane, and the llm-d router and model servers.
_SERVING_NAMESPACES = ("metallb-system", "agentgateway-system", "llm-d")


def _unready_serving_pods(runtime: LabRuntime) -> dict[str, list[str] | None]:
    """Return unready pods per serving namespace (``None``: no pods listed)."""
    result = {}
    for namespace in _SERVING_NAMESPACES:
        unready = k8s_unready_pods(
            exec_or_empty(
                runtime,
                "controller",
                f"kubectl get pods -n {namespace} --no-headers",
                timeout=60,
            )
        )
        if unready is None or unready:
            result[namespace] = unready
    return result


def verify_llmd_lab_startup(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    """Fast readiness for inject: nodes/MetalLB plus llm-d gateway HTTP.

    Workload-scoped faults (e.g. NetworkPolicy deny in ``llm-d``) inject right
    after ``start_net_env`` returns, so startup must wait for the llm-d
    namespace and models path — not only for Ready k3s nodes.
    """
    nodes = exec_or_empty(
        runtime, "controller", "kubectl get nodes --no-headers", timeout=60
    )
    ready_nodes = k8s_ready_node_count(nodes)
    ns_phase = exec_or_empty(
        runtime,
        "controller",
        "kubectl get ns llm-d -o jsonpath={.status.phase}",
        timeout=60,
    )
    gateway_addr = exec_or_empty(
        runtime,
        "controller",
        "kubectl get gateway -n llm-d llm-d-gateway "
        "-o jsonpath={.status.addresses[0].value}",
        timeout=60,
    ).strip()
    sync_llmd_client_hosts(runtime)
    models_url = models_http_url(gateway_addr)
    checks = {
        "nodes_deployed": nodes_deployed(runtime, _LLMD_EXPECTED),
        "controller_ipv4": host_has_ipv4(runtime, "controller", "200.0.0.1"),
        "client_reaches_controller": ping_ok(runtime, "client", "200.0.0.1", count=3),
        "k3s_nodes_ready": ready_nodes >= 6,
        "metallb_ready": "Running"
        in exec_or_empty(
            runtime,
            "controller",
            "kubectl get pods -n metallb-system --no-headers",
            timeout=60,
        ),
        "llm_d_namespace": k8s_namespace_phase_active(ns_phase),
        "gateway_addressed": gateway_addr.startswith("200.0.0."),
        "models_http": http_ok(runtime, "client", models_url),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "ready_nodes": ready_nodes,
            "gateway_addr": gateway_addr,
            "models_url": models_url,
        },
    )


def verify_llmd_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    """Startup checks plus every k3s node reachable and every serving pod ready."""
    startup = verify_llmd_lab_startup(runtime, scenario_name=scenario_name)
    unreachable = ping_unreachable(runtime, "client", _K3S_NODE_ADDRESSES)
    unready = _unready_serving_pods(runtime)
    checks = {
        **startup["checks"],
        "client_ipv4": host_has_ipv4(runtime, "client", "200.0.0.7"),
        "client_reaches_k3s_nodes": not unreachable,
        "serving_pods_ready": not unready,
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            **startup["details"],
            "failures": {
                "unreachable_k3s_nodes": unreachable,
                "unready_pods": unready,
            },
        },
    )
