"""Kubernetes symptom probes (test-path only).

ClusterIP routing and CoreDNS isolation are observed through cluster DNS: a
node resolves a cluster name through the kube-dns virtual IP, as its pods do.
The ClusterIP control queries the CoreDNS pod directly from the same node and
the VIP from another node. The CoreDNS observer is a node that does not host
CoreDNS, and its control opens the CoreDNS pod's metrics port by IP.

Worker partition:

The worker reports NotReady, the node lifecycle controller marks its pods not
ready so EndpointSlices withdraw them, and ``kubectl logs`` to a pod on it
fails while the node still answers ping. The same logs query to a pod on a
healthy node separates the symptom from a broken kubectl path. ``kubectl logs``
is used instead of ``exec`` because distroless images have no binary to exec.
"""

from __future__ import annotations

from typing import Any

from nika.problems.base import build_verify_result
from nika.problems.management_orchestration_plane.kubernetes import (
    LOGS_REQUEST_TIMEOUT_SEC,
)
from nika.problems.support.kubernetes.node_filter import NodeFilter
from nika.service.lab.k8s_api import K8sCommandError


def _running_pods(k8s: Any, control: str) -> list[dict[str, Any]]:
    pods = []
    for item in k8s.kubectl_json(control, "get pods -A").get("items", []):
        status = item.get("status") or {}
        if status.get("phase") != "Running":
            continue
        pods.append(
            {
                "name": item["metadata"]["name"],
                "namespace": item["metadata"]["namespace"],
                "node": (item.get("spec") or {}).get("nodeName", ""),
                "ready": any(
                    cond.get("type") == "Ready" and cond.get("status") == "True"
                    for cond in status.get("conditions") or []
                ),
            }
        )
    return sorted(pods, key=lambda pod: (pod["namespace"], pod["name"]))


def _logs(k8s: Any, control: str, pod: dict[str, Any] | None) -> dict[str, Any]:
    if pod is None:
        return {"pod": None, "ok": None}
    result = k8s.kubectl(
        control,
        f"logs {pod['name']} -n {pod['namespace']} --all-containers --tail=1",
        timeout=LOGS_REQUEST_TIMEOUT_SEC,
        check=False,
    )
    return {
        "pod": f"{pod['namespace']}/{pod['name']}",
        "ok": result.ok,
        "stderr": result.stderr[:200],
    }


def _sample(problem: Any, params: Any) -> dict[str, Any]:
    k8s = problem.runtime.lab_api
    control = problem.control_node(params)
    node_name = k8s.k8s_node_for_device(
        control, problem._target_device(params), devices=problem.cluster_nodes()
    )
    nodes = {entry["name"]: entry for entry in k8s.k8s_nodes(control)}
    target = nodes.get(node_name) or {}
    pods = _running_pods(k8s, control)
    on_node = [pod for pod in pods if pod["node"] == node_name]
    healthy_pod = next(
        (
            pod
            for pod in pods
            if pod["node"] != node_name
            and pod["ready"]
            and nodes.get(pod["node"], {}).get("ready")
        ),
        None,
    )
    slices = k8s.kubectl_json(control, "get endpointslices -A").get("items", [])
    endpoint_ready = [
        (endpoint.get("conditions") or {}).get("ready") is not False
        for item in slices
        for endpoint in item.get("endpoints") or []
        if endpoint.get("nodeName") == node_name
    ]
    node_ip = target.get("internal_ip")
    return {
        "k8s_node": node_name,
        "node_ready": target.get("ready"),
        "node_ip": node_ip,
        "node_pingable": bool(node_ip)
        and problem.runtime.ping_ok(control, node_ip, count=2),
        "pods_on_node": len(on_node),
        "pods_ready_on_node": sum(pod["ready"] for pod in on_node),
        "node_endpoints": len(endpoint_ready),
        "node_endpoints_ready": sum(endpoint_ready),
        "logs": _logs(k8s, control, on_node[0] if on_node else None),
        "control_logs": _logs(k8s, control, healthy_pod),
    }


def worker_partition_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    try:
        sample = _sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    healthy = bool(
        sample["node_ready"] is True
        and sample["node_pingable"]
        and sample["pods_ready_on_node"] > 0
        and (sample["node_endpoints"] == 0 or sample["node_endpoints_ready"] > 0)
        and sample["logs"]["ok"] is True
        and sample["control_logs"]["ok"] is True
    )
    return healthy, sample


def worker_partition(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """NotReady worker whose pods leave endpoints and refuse logs, still pingable."""
    try:
        sample = _sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    control_ok = sample["control_logs"]["ok"] is True
    verified = bool(
        sample["node_ready"] is False
        and sample["node_pingable"]
        and sample["pods_on_node"] > 0
        and sample["pods_ready_on_node"] == 0
        and sample["node_endpoints_ready"] == 0
        and control_ok
        and sample["logs"]["ok"] is False
    )
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok},
    )


# Cluster DNS is the one ClusterIP Service every k3s lab runs, so a name lookup
# through its virtual IP is the Service-routing request a workload makes first.
_DNS_SERVICE = ("kube-dns", "kube-system")
_DNS_METRICS_PORT = 9153
_LOOKUPS = 3


def _cluster_dns(problem: Any, params: Any) -> dict[str, Any]:
    k8s = problem.runtime.lab_api
    control = problem.control_node(params)
    name, namespace = _DNS_SERVICE
    return {
        "cluster_ip": k8s.k8s_service_cluster_ip(control, name, namespace=namespace),
        "pod_ips": k8s.k8s_service_endpoint_addresses(
            control, name, namespace=namespace
        ),
    }


def _lookups(problem: Any, node: str, server: str) -> int:
    """Successful lookups of a cluster name from ``node``'s own network stack."""
    k8s = problem.runtime.lab_api
    return sum(k8s.k8s_host_dns_ok(node, server) for _ in range(_LOOKUPS))


def _clusterip_sample(problem: Any, params: Any) -> dict[str, Any]:
    if params.service_name:
        return {"error": "single_service_mode_has_no_dns_probe"}
    target = problem._target_device(params)
    peer = next((n for n in sorted(problem.cluster_nodes()) if n != target), None)
    dns = _cluster_dns(problem, params)
    if not dns["cluster_ip"] or not dns["pod_ips"] or peer is None:
        return {"error": "no_cluster_dns_probe", **dns, "peer": peer}
    return {
        "node": target,
        "peer": peer,
        **dns,
        "node_via_cluster_ip": _lookups(problem, target, dns["cluster_ip"]),
        "node_via_pod_ip": _lookups(problem, target, dns["pod_ips"][0]),
        "peer_via_cluster_ip": _lookups(problem, peer, dns["cluster_ip"]),
        "lookups": _LOOKUPS,
    }


def clusterip_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    try:
        sample = _clusterip_sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    healthy = "error" not in sample and all(
        sample[key] == _LOOKUPS
        for key in ("node_via_cluster_ip", "node_via_pod_ip", "peer_via_cluster_ip")
    )
    return healthy, sample


def clusterip_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The node cannot use a Service VIP; its backend and other nodes still can."""
    try:
        sample = _clusterip_sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    if "error" in sample:
        return False, sample
    control_ok = (
        sample["node_via_pod_ip"] == _LOOKUPS
        and sample["peer_via_cluster_ip"] == _LOOKUPS
    )
    verified = sample["node_via_cluster_ip"] == 0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok},
    )


def _coredns_sample(problem: Any, params: Any) -> dict[str, Any]:
    k8s = problem.runtime.lab_api
    hosting = set(problem._dns_pod_nodes(params, k8s))
    candidates = [params.symptom_host] if params.symptom_host else []
    candidates += sorted(problem.cluster_nodes())
    observer = next((n for n in candidates if n and n not in hosting), None)
    dns = _cluster_dns(problem, params)
    if observer is None or not dns["cluster_ip"] or not dns["pod_ips"]:
        return {"error": "no_dns_observer", **dns, "coredns_nodes": sorted(hosting)}
    return {
        "observer": observer,
        "coredns_nodes": sorted(hosting),
        **dns,
        "lookups_ok": _lookups(problem, observer, dns["cluster_ip"]),
        "lookups": _LOOKUPS,
        # The same CoreDNS pod answers TCP on its metrics port by IP.
        "pod_reachable": NodeFilter(problem.runtime, observer).tcp_reachable(
            dns["pod_ips"][0], _DNS_METRICS_PORT
        ),
    }


def coredns_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    try:
        sample = _coredns_sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    healthy = (
        "error" not in sample
        and sample["lookups_ok"] == _LOOKUPS
        and sample["pod_reachable"] is True
    )
    return healthy, sample


def coredns_isolated(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Another node's lookups fail while the CoreDNS pod stays reachable by IP."""
    try:
        sample = _coredns_sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    if "error" in sample:
        return False, sample
    control_ok = sample["pod_reachable"] is True
    verified = sample["lookups_ok"] == 0 and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok},
    )
