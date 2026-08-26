"""Startup verification signals for the llmd_lab scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    host_has_ipv4,
    http_ok,
    k8s_ready_node_count,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime


def verify_llmd_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
    expected = (
        "controller",
        "worker1",
        "worker2",
        "worker3",
        "worker4",
        "worker5",
        "client",
    )
    nodes = exec_or_empty(
        runtime, "controller", "kubectl get nodes --no-headers", timeout=60
    )
    ready_nodes = k8s_ready_node_count(nodes)
    gateway_addr = exec_or_empty(
        runtime,
        "controller",
        "kubectl get gateway -n llm-d llm-d-gateway "
        "-o jsonpath={.status.addresses[0].value}",
        timeout=60,
    ).strip()
    agentgateway = exec_or_empty(
        runtime,
        "controller",
        "kubectl get pods -n agentgateway-system --no-headers",
        timeout=60,
    )
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "controller_ipv4": host_has_ipv4(runtime, "controller", "200.0.0.1"),
        "client_ipv4": host_has_ipv4(runtime, "client", "200.0.0.7"),
        "client_reaches_controller": ping_ok(runtime, "client", "200.0.0.1", count=3),
        "k3s_nodes_ready": ready_nodes >= 6,
        "metallb_ready": "Running"
        in exec_or_empty(
            runtime,
            "controller",
            "kubectl get pods -n metallb-system --no-headers",
            timeout=60,
        ),
        "agentgateway_ready": "Running" in agentgateway,
        "gateway_addressed": gateway_addr.startswith("200.0.0."),
        "models_http": http_ok(runtime, "client", "http://llmd/v1/models"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={"ready_nodes": ready_nodes, "gateway_addr": gateway_addr},
    )
