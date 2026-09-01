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
    frr_bgp_established,
    host_has_ipv4,
    http_ok,
    k8s_namespace_phase_active,
    k8s_ready_node_count,
    nodes_deployed,
    ping_ok,
)
from nika.runtime.base import LabRuntime

_K8S_EXPECTED = (
    "controller",
    "worker1",
    "worker2",
    "worker3",
    "worker4",
    "worker5",
    "client",
)


def verify_k8s_lab_startup(
    runtime: LabRuntime, *, scenario_name: str
) -> dict[str, Any]:
    """Fast readiness for inject: nodes up plus sample app namespaces/HTTP.

    Workload-scoped faults (e.g. NetworkPolicy deny on ``word-ns``) inject
    immediately after ``start_net_env`` returns, so startup must wait until
    controller.startup has applied sample namespaces and both sample ingress
    paths answer — not only until k3s nodes report Ready.
    """
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
    sync_k8s_client_hosts(runtime)
    word_url = word_app_http_url(ingress_ip)
    weather_url = weather_app_http_url(ingress_ip)
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
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "ready_nodes": ready_nodes,
            "ingress_ip": ingress_ip,
            "word_url": word_url,
            "weather_url": weather_url,
        },
    )


def verify_k8s_lab(runtime: LabRuntime, *, scenario_name: str) -> dict[str, Any]:
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
    sync_k8s_client_hosts(runtime)
    word_url = word_app_http_url(ingress_ip)
    checks = {
        "nodes_deployed": nodes_deployed(runtime, _K8S_EXPECTED),
        "controller_ipv4": host_has_ipv4(runtime, "controller", "201.1.1.2"),
        "worker3_reachable": ping_ok(runtime, "controller", "201.2.1.2", count=3),
        "client_reaches_controller": ping_ok(runtime, "client", "201.1.1.2", count=3),
        "k3s_nodes_ready": ready_nodes >= 6,
        "ingress_vip_allocated": ingress_ip.startswith("101."),
        "leaf_bgp_established": frr_bgp_established(runtime, "leaf_1_1"),
        "word_app_http": http_ok(runtime, "client", word_url),
    }
    return build_lab_verify_result(
        scenario_name=scenario_name,
        verified=all(checks.values()),
        checks=checks,
        details={
            "ingress_ip": ingress_ip,
            "ready_nodes": ready_nodes,
            "word_url": word_url,
        },
    )
