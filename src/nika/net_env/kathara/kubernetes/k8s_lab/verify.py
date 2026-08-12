"""Startup verification signals for the k8s_lab scenario."""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import (
    build_lab_verify_result,
    exec_or_empty,
    frr_bgp_established,
    host_has_ipv4,
    http_ok,
    k8s_ready_node_count,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime


def verify_k8s_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
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
    ingress_ip = exec_or_empty(
        runtime,
        "controller",
        "kubectl get svc -n ingress-nginx ingress-nginx-controller "
        "-o jsonpath={.status.loadBalancer.ingress[0].ip}",
        timeout=60,
    ).strip()
    checks = {
        "nodes_deployed": nodes_deployed(runtime, expected),
        "controller_ipv4": host_has_ipv4(runtime, "controller", "201.1.1.2"),
        "worker3_reachable": ping_ok(runtime, "controller", "201.2.1.2", count=3),
        "client_reaches_controller": ping_ok(runtime, "client", "201.1.1.2", count=3),
        "k3s_nodes_ready": ready_nodes >= 6,
        "ingress_vip_allocated": ingress_ip.startswith("101."),
        "leaf_bgp_established": frr_bgp_established(runtime, "leaf_1_1"),
        "word_app_http": http_ok(runtime, "client", "http://datacenter.com/word"),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "ingress_ip": ingress_ip,
            "ready_nodes": ready_nodes,
        },
    )
