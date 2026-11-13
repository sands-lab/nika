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

VXLAN overlay neighbor:

Pod IPs on one remote node are unreachable from the node whose overlay
neighbor for that VTEP is stale. The same pod IPs still answer from a third
node, and the affected node still reaches pods on a different remote node.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any

from nika.problems.base import build_verify_result
from nika.problems.management_orchestration_plane.kubernetes import (
    LOGS_REQUEST_TIMEOUT_SEC,
    _SYSTEM_NAMESPACES,
)
from nika.problems.support.kubernetes.node_filter import NodeFilter, NodeFilterError
from nika.problems.support.kubernetes.objects import ingress_backends, traffic_backends
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


def _pod_ip_on_device(problem: Any, params: Any, device: str) -> str | None:
    """A Running, non-hostNetwork pod IP scheduled on ``device``."""
    k8s = problem.runtime.lab_api
    control = problem.control_node(params)
    k8s_node = k8s.k8s_node_for_device(control, device, devices=problem.cluster_nodes())
    preferred: list[str] = []
    other: list[str] = []
    for item in k8s.kubectl_json(control, "get pods -A").get("items", []):
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        if spec.get("hostNetwork") or status.get("phase") != "Running":
            continue
        if spec.get("nodeName") != k8s_node:
            continue
        ip = status.get("podIP") or ""
        if not ip:
            continue
        ns = (item.get("metadata") or {}).get("namespace") or ""
        if ns == "monitoring":
            preferred.append(ip)
        elif ns not in {"kube-system", "metallb-system"}:
            other.append(ip)
    return (preferred or other or [None])[0]


def _overlay_sample(problem: Any, params: Any) -> dict[str, Any]:
    node = params.node_name
    peer = params.peer_name
    others = sorted(
        name for name in problem.cluster_nodes() if name not in {node, peer}
    )
    control_src = others[0] if others else None
    control_dst_node = others[1] if len(others) > 1 else control_src
    if not node or not peer or control_src is None:
        return {"error": "no_overlay_probe_pair", "node": node, "peer": peer}
    try:
        peer_pod = _pod_ip_on_device(problem, params, peer)
        other_pod = _pod_ip_on_device(problem, params, control_dst_node)
    except K8sCommandError as exc:
        return {"error": str(exc), "node": node, "peer": peer}
    if not peer_pod or not other_pod:
        return {
            "error": "no_pod_ip",
            "node": node,
            "peer": peer,
            "peer_pod": peer_pod,
            "other_pod": other_pod,
        }
    runtime = problem.runtime
    return {
        "node": node,
        "peer": peer,
        "control_src": control_src,
        "peer_pod": peer_pod,
        "other_pod": other_pod,
        "node_to_peer": runtime.ping_ok(node, peer_pod, count=2),
        "node_to_other": runtime.ping_ok(node, other_pod, count=2),
        "control_to_peer": runtime.ping_ok(control_src, peer_pod, count=2),
    }


def vxlan_neighbor_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    try:
        sample = _overlay_sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    healthy = "error" not in sample and all(
        sample[key] is True
        for key in ("node_to_peer", "node_to_other", "control_to_peer")
    )
    return healthy, sample


def vxlan_neighbor_stale(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """One overlay pair is lost; the affected node and a control pair still reach others."""
    try:
        sample = _overlay_sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    if "error" in sample:
        return False, sample
    control_ok = sample["node_to_other"] is True and sample["control_to_peer"] is True
    verified = sample["node_to_peer"] is False and control_ok
    return verified, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=verified,
        details={**sample, "control_ok": control_ok},
    )


# ------------------------------------------------ service entry points

_HTTP_TIMEOUT_SEC = 5
_HTTP_STATUS_MARKER = "__http_status="
_ENTRY_WAIT_SEC = 90.0


def _client(problem: Any) -> str:
    from nika.net_env.net_env_pool import get_probe_path

    path = get_probe_path(problem.scenario_name)
    return getattr(path, "src_host", None) or "client"


def _http_code(problem: Any, host: str, url: str) -> str:
    """HTTP status ``url`` returns to ``host``; ``000`` when the request fails."""
    return _http_exchange(problem, host, _backend_request(problem, url))[0]


@dataclass(frozen=True)
class HttpProbeRequest:
    """One user-facing HTTP request used by Service-entry symptom probes."""

    method: str = "GET"
    url: str = ""
    body: str | None = None
    content_type: str | None = None
    success_contains: str = ""

    def succeeded(self, status: str, body: str = "") -> bool:
        if not status.isdigit():
            return False
        code = int(status)
        if self.success_contains:
            return 200 <= code < 300 and self.success_contains in body
        return 100 <= code < 500


def _backend_request(problem: Any, url: str) -> HttpProbeRequest:
    net_env = getattr(problem, "net_env", None)
    builder = getattr(net_env, "http_backend_request", None) if net_env else None
    if not callable(builder):
        return HttpProbeRequest(url=url)
    spec = builder(url)
    return HttpProbeRequest(
        method=spec.get("method") or "GET",
        url=spec.get("url") or url,
        body=spec.get("body"),
        content_type=spec.get("content_type"),
        success_contains=spec.get("success_contains") or "",
    )


def _http_exchange(
    problem: Any, host: str, request: HttpProbeRequest
) -> tuple[str, str]:
    extra = ""
    if request.method != "GET":
        extra += f" -X {shlex.quote(request.method)}"
    if request.body is not None:
        content_type = request.content_type or "application/json"
        extra += f" -H {shlex.quote(f'Content-Type: {content_type}')}"
        extra += f" --data-binary {shlex.quote(request.body)}"
    output_option = "" if request.success_contains else " -o /dev/null"
    try:
        output = problem.runtime.exec(
            host,
            f"curl -s{output_option} "
            f"-m {_HTTP_TIMEOUT_SEC} -w '\\n{_HTTP_STATUS_MARKER}%{{http_code}}'{extra} "
            f"{shlex.quote(request.url)}",
            timeout=_HTTP_TIMEOUT_SEC + 10,
        )
    except Exception as exc:  # noqa: BLE001 - runtime transports use different errors
        raise K8sCommandError(f"HTTP probe on {host!r} failed: {exc}") from exc
    body, separator, status = output.rpartition(_HTTP_STATUS_MARKER)
    status = status.strip()
    if (
        output.startswith("[TIMEOUT]")
        or not separator
        or len(status) != 3
        or not status.isdigit()
    ):
        raise K8sCommandError(
            f"Invalid HTTP probe output on {host!r}: {output[:200]!r}"
        )
    return status, body.rstrip("\n")


def _wait_for(check, timeout: float = _ENTRY_WAIT_SEC):
    import time

    deadline = time.monotonic() + timeout
    while True:
        try:
            ok, details = check()
        except (K8sCommandError, NodeFilterError) as exc:
            ok, details = False, {"error": str(exc)}
        if ok or time.monotonic() >= deadline:
            return ok, details
        time.sleep(5.0)


def _service_urls(problem: Any) -> dict[str, str]:
    """``namespace/service`` -> the URL that depends on that Service."""
    k8s = problem.runtime.lab_api
    control = problem.control_node()
    return {
        f"{namespace}/{service}": url
        for namespace, service, url in traffic_backends(k8s, control)
    }


def _service_probes(problem: Any) -> dict[str, HttpProbeRequest]:
    """``namespace/service`` -> the user-facing request that depends on that Service."""
    return {
        ref: _backend_request(problem, url)
        for ref, url in _service_urls(problem).items()
    }


def _answered(code: str) -> bool:
    """The backend answered: any status below 500 (a bare path may return 4xx)."""
    return code.isdigit() and 100 <= int(code) < 500


def target_port_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every Ingress or business-traffic URL gets an answer from its backend."""
    client = _client(problem)
    probes = _service_probes(problem)
    results = {
        ref: _http_exchange(problem, client, probe) for ref, probe in probes.items()
    }
    codes = {ref: status for ref, (status, _body) in results.items()}
    problem._audit_service_codes = codes
    problem._audit_service_probes = probes
    ok = bool(results) and all(
        probes[ref].succeeded(status, body) for ref, (status, body) in results.items()
    )
    return ok, {"client": client, "codes": codes}


def target_port_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The changed Service's URL gets no answer; the other URLs answer as before."""
    client = _client(problem)
    probes = getattr(problem, "_audit_service_probes", None) or _service_probes(problem)
    before = getattr(problem, "_audit_service_codes", None) or {}
    target = problem.service_ref

    def check():
        results = {
            ref: _http_exchange(problem, client, probe) for ref, probe in probes.items()
        }
        codes = {ref: status for ref, (status, _body) in results.items()}
        others_same = all(
            codes[ref] == before.get(ref) for ref in codes if ref != target
        )
        status, body = results.get(target, ("000", ""))
        ok = (
            target in results
            and not probes[target].succeeded(status, body)
            and others_same
        )
        return ok, {
            "client": client,
            "service": target,
            "before": before,
            "codes": codes,
        }

    ok, details = _wait_for(check)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )


def _selector_sample(problem: Any) -> dict[str, Any]:
    k8s = problem.runtime.lab_api
    control = problem.control_node()
    namespace, name = (problem.service_ref or "/").split("/", 1)
    endpoints = k8s.k8s_service_endpoint_addresses(control, name, namespace=namespace)
    original = getattr(problem, "_original", None) or {}
    selector = ((original.get("spec") or {}).get("selector") or {}) if original else {}
    selector_s = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pods = (
        k8s.k8s_pods(control, namespace=namespace, selector=selector_s)
        if selector_s
        else []
    )
    return {
        "endpoints": sorted(endpoints),
        "pods": len(pods),
        "pods_ready": bool(pods)
        and all(pod.get("ready") and pod.get("phase") == "Running" for pod in pods),
    }


def selector_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The Service has no endpoints; its URL fails; the original Pods stay Ready."""
    client = _client(problem)
    probes = getattr(problem, "_audit_service_probes", None) or _service_probes(problem)
    before = getattr(problem, "_audit_service_codes", None) or {}
    target = problem.service_ref

    def check():
        results = {
            ref: _http_exchange(problem, client, probe) for ref, probe in probes.items()
        }
        codes = {ref: status for ref, (status, _body) in results.items()}
        sample = _selector_sample(problem)
        others_same = all(
            codes[ref] == before.get(ref) for ref in codes if ref != target
        )
        status, body = results.get(target, ("000", ""))
        ok = (
            target in results
            and not probes[target].succeeded(status, body)
            and others_same
            and not sample["endpoints"]
            and sample["pods_ready"]
        )
        return ok, {
            "client": client,
            "service": target,
            "before": before,
            "codes": codes,
            **sample,
        }

    ok, details = _wait_for(check)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )


def _load_balancer_ips(problem: Any) -> list[str]:
    k8s = problem.runtime.lab_api
    items = k8s.kubectl_json(problem.control_node(), "get services -A").get("items", [])
    return sorted(
        ingress["ip"]
        for item in items
        if item["spec"].get("type") == "LoadBalancer"
        for ingress in (
            item.get("status", {}).get("loadBalancer", {}).get("ingress") or []
        )
        if ingress.get("ip")
    )


def _vip_reachable(problem: Any, client: str, ip: str) -> bool:
    # Any HTTP status, even 404, means the address answered.
    return _http_code(problem, client, f"http://{ip}/") != "000"


def lb_advertisement_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Every LoadBalancer IP answers the external client."""
    client = _client(problem)
    ips = _load_balancer_ips(problem)
    reachable = {ip: _vip_reachable(problem, client, ip) for ip in ips}
    problem._audit_lb_ips = ips
    return bool(ips) and all(reachable.values()), {
        "client": client,
        "reachable": reachable,
    }


def _flush_neighbors(problem: Any, client: str, ips: list[str]) -> None:
    """Drop cached neighbor entries so the next probe needs a fresh ARP/ND."""
    for ip in ips:
        problem.runtime.exec(client, f"ip neigh flush to {ip}", timeout=10)


def lb_advertisement_withdrawn(
    problem: Any, params: Any
) -> tuple[bool, dict[str, Any]]:
    """The LoadBalancer IPs keep their assignment but stop answering outside clients."""
    client = _client(problem)
    baseline = getattr(problem, "_audit_lb_ips", None) or []

    def check():
        _flush_neighbors(problem, client, baseline)
        ips = _load_balancer_ips(problem)
        reachable = {ip: _vip_reachable(problem, client, ip) for ip in baseline}
        ok = (
            bool(baseline) and set(baseline) <= set(ips) and not any(reachable.values())
        )
        return ok, {"client": client, "assigned": ips, "reachable": reachable}

    ok, details = _wait_for(check)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )


def _ingress_class_sample(problem: Any) -> dict[str, Any]:
    k8s = problem.runtime.lab_api
    control = problem.control_node()
    client = _client(problem)
    routes = ingress_backends(k8s, control)
    codes = {
        f"http://{host}{path}": _http_code(problem, client, f"http://{host}{path}")
        for _, _, host, path in routes
    }
    backends = {}
    for namespace, name in sorted({(ns, svc) for ns, svc, _, _ in routes}):
        service = k8s.kubectl_json(control, f"get service {name} -n {namespace}")
        endpoints = k8s.k8s_service_endpoint_addresses(
            control, name, namespace=namespace
        )
        ip = service["spec"]["clusterIP"]
        port = service["spec"]["ports"][0]["port"]
        backends[f"{namespace}/{name}"] = {
            "endpoints": sorted(endpoints),
            "service_spec": service["spec"],
            "reachable": NodeFilter(problem.runtime, control).tcp_reachable(ip, port),
        }
    return {"client": client, "codes": codes, "backends": backends}


def ingress_class_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Record working routes and directly reachable Service backends."""

    def check():
        sample = _ingress_class_sample(problem)
        ok = (
            bool(sample["codes"])
            and all(
                _answered(code) and code != "404" for code in sample["codes"].values()
            )
            and all(
                backend["endpoints"] and backend["reachable"] is True
                for backend in sample["backends"].values()
            )
        )
        return ok, sample

    ok, sample = _wait_for(check)
    problem._audit_ingress_class = sample
    return ok, sample


def ingress_class_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The selected Ingress returns 404; its Services still answer directly."""
    before = getattr(problem, "_audit_ingress_class", None) or {}
    original = problem._original
    targets = {
        f"http://{rule.get('host', '')}{path.get('path', '/')}"
        for rule in original["spec"].get("rules", [])
        for path in (rule.get("http") or {}).get("paths", [])
        if (path.get("backend") or {}).get("service")
    }

    def check():
        sample = _ingress_class_sample(problem)
        codes = sample["codes"]
        ok = (
            bool(targets)
            and targets <= codes.keys()
            and all(codes[url] == "404" for url in targets)
            and all(
                code == before.get("codes", {}).get(url)
                for url, code in codes.items()
                if url not in targets
            )
            and bool(sample["backends"])
            and sample["backends"] == before.get("backends")
        )
        return ok, {
            **sample,
            "ingress": problem.ingress_ref,
            "targets": sorted(targets),
        }

    ok, details = _wait_for(check)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )


def _deployment_sample(problem: Any) -> dict[str, Any]:
    k8s = problem.runtime.lab_api
    control = problem.control_node()
    namespace, name = (problem.deployment_ref or "/").split("/", 1)
    if not name:
        return {"replicas": None, "pods": 0, "ready_pods": 0}
    deploy = k8s.kubectl_json(control, f"get deploy {name} -n {namespace}")
    selector = ((deploy.get("spec") or {}).get("selector") or {}).get(
        "matchLabels"
    ) or {}
    selector_s = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pods = (
        k8s.k8s_pods(control, namespace=namespace, selector=selector_s)
        if selector_s
        else []
    )
    replicas = (deploy.get("spec") or {}).get("replicas")
    return {
        "replicas": int(replicas) if replicas is not None else 0,
        "pods": len(pods),
        "ready_pods": sum(
            1 for pod in pods if pod.get("ready") and pod.get("phase") == "Running"
        ),
    }


def replicas_zero_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Traffic URLs answer and the target Deployment still has replicas."""
    if not problem.deployment_ref:
        namespace, name, service_ns, service = problem._targets(params)
        problem.deployment_ref = f"{namespace}/{name}"
        problem.service_ref = f"{service_ns}/{service}" if service else None
    ok, sample = target_port_baseline(problem, params)
    deploy = _deployment_sample(problem)
    return ok and bool(deploy["replicas"]), {**sample, **deploy}


def replicas_zero_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The Deployment has no replicas or Pods; its URL fails; others still answer."""
    client = _client(problem)
    probes = getattr(problem, "_audit_service_probes", None) or _service_probes(problem)
    before = getattr(problem, "_audit_service_codes", None) or {}
    target = problem.service_ref

    def check():
        results = {
            ref: _http_exchange(problem, client, probe) for ref, probe in probes.items()
        }
        codes = {ref: status for ref, (status, _body) in results.items()}
        sample = _deployment_sample(problem)
        others_same = all(
            codes[ref] == before.get(ref) for ref in codes if ref != target
        )
        status, body = results.get(target, ("000", ""))
        ok = (
            target in results
            and not probes[target].succeeded(status, body)
            and others_same
            and sample["replicas"] == 0
            and sample["pods"] == 0
            and sample["ready_pods"] == 0
        )
        return ok, {
            "client": client,
            "service": target,
            "deployment": problem.deployment_ref,
            "before": before,
            "codes": codes,
            **sample,
        }

    ok, details = _wait_for(check)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )


def _pending_business_pods(problem: Any) -> list[dict[str, Any]]:
    k8s = problem.runtime.lab_api
    control = problem.control_node()
    pending = []
    for item in k8s.kubectl_json(control, "get pods -A").get("items", []):
        namespace = (item.get("metadata") or {}).get("namespace") or ""
        if namespace in _SYSTEM_NAMESPACES:
            continue
        if (item.get("status") or {}).get("phase") != "Pending":
            continue
        conditions = (item.get("status") or {}).get("conditions") or [{}]
        pending.append(
            {
                "name": (item.get("metadata") or {}).get("name"),
                "namespace": namespace,
                "reason": conditions[-1].get("reason"),
            }
        )
    return pending


def noexecute_taint_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """Traffic URLs answer and the node does not carry the injected taint."""
    ok, sample = target_port_baseline(problem, params)
    present = problem._taint_present(params)
    pending = _pending_business_pods(problem)
    return ok and not present and not pending, {
        **sample,
        "taint_present": present,
        "pending": pending,
    }


def noexecute_taint_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    """The taint is present; one application path fails; a sibling path still answers."""
    client = _client(problem)
    urls = _service_urls(problem)
    before = getattr(problem, "_audit_service_codes", None) or {}

    def check():
        codes = {ref: _http_code(problem, client, url) for ref, url in urls.items()}
        pending = _pending_business_pods(problem)
        failed = [
            ref
            for ref, code in codes.items()
            if before.get(ref) and _answered(before[ref]) and not _answered(code)
        ]
        still_ok = [
            ref
            for ref, code in codes.items()
            if before.get(ref) and _answered(before[ref]) and _answered(code)
        ]
        taint_present = problem._taint_present(params)
        ok = taint_present and bool(failed) and bool(still_ok) and bool(pending)
        return ok, {
            "client": client,
            "node": problem.k8s_node,
            "taint": problem.taint,
            "taint_present": taint_present,
            "before": before,
            "codes": codes,
            "failed": failed,
            "still_ok": still_ok,
            "pending": pending,
        }

    ok, details = _wait_for(check)
    return ok, build_verify_result(
        fault_type=problem.root_cause_name, verified=ok, details=details
    )


def readiness_sample(problem: Any, params: Any) -> dict[str, Any]:
    """Count running/ready Pods, EndpointSlice readiness, and kubelet failures."""
    k8s = problem.runtime.lab_api
    control = problem.control_node(params)
    namespace, name = problem.deployment_ref.split("/", 1)
    deploy = k8s.kubectl_json(control, f"get deploy {name} -n {namespace}")
    selector = deploy["spec"]["selector"]["matchLabels"]
    labels = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
    pods = k8s.k8s_pods(control, namespace=namespace, selector=labels)
    service_ns, service = problem.service_ref.split("/", 1)
    slices = k8s.kubectl_json(
        control,
        f"get endpointslices -n {service_ns} -l kubernetes.io/service-name={service}",
    )
    endpoints = [
        ep for item in slices.get("items") or [] for ep in item.get("endpoints") or []
    ]
    events = k8s.kubectl_json(control, f"get events -n {namespace}")
    names = {pod["name"] for pod in pods}
    failures = [
        event.get("message", "")
        for event in events.get("items", [])
        if (event.get("involvedObject") or {}).get("name") in names
        and "Readiness probe failed" in event.get("message", "")
    ]
    return {
        "replicas": int(deploy["spec"].get("replicas") or 0),
        "pods": len(pods),
        "ready_pods": sum(
            pod.get("ready") and pod.get("phase") == "Running" for pod in pods
        ),
        "running_pods": sum(pod.get("phase") == "Running" for pod in pods),
        "ready_endpoints": sum(
            (ep.get("conditions") or {}).get("ready") is True for ep in endpoints
        ),
        "not_ready_endpoints": sum(
            (ep.get("conditions") or {}).get("ready") is False for ep in endpoints
        ),
        "readiness_failures": failures,
        "nodes_ready": all(node.get("ready") for node in k8s.k8s_nodes(control)),
    }


def _readiness_invariants(problem: Any, params: Any) -> dict[str, Any]:
    k8s = problem.runtime.lab_api
    control = problem.control_node(params)
    return {
        resource: {
            f"{item['metadata']['namespace']}/{item['metadata']['name']}": item["spec"]
            for item in k8s.kubectl_json(control, f"get {resource} -A").get("items", [])
        }
        for resource in ("services", "ingresses")
    }


def readiness_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    namespace, name, service_ns, service = problem._targets(params)
    problem.deployment_ref = f"{namespace}/{name}"
    problem.service_ref = f"{service_ns}/{service}"
    problem._audit_readiness_invariants = _readiness_invariants(problem, params)
    ok, traffic = target_port_baseline(problem, params)
    sample = readiness_sample(problem, params)
    healthy = (
        sample["replicas"] > 0
        and sample["ready_pods"] == sample["replicas"]
        and sample["ready_endpoints"] > 0
        and sample["nodes_ready"]
    )
    return ok and healthy, {**traffic, **sample}


def readiness_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    def check():
        sample = readiness_sample(problem, params)
        ok = (
            sample["pods"] >= sample["replicas"] > 0
            and sample["running_pods"] == sample["pods"]
            and sample["ready_pods"] == 0
            and sample["ready_endpoints"] == 0
            and sample["not_ready_endpoints"] >= sample["replicas"]
            and bool(sample["readiness_failures"])
            and sample["nodes_ready"]
        )
        return ok, sample

    ok, details = _wait_for(check, timeout=900)
    traffic_ok, traffic = target_port_broken(problem, params)
    unchanged = (
        _readiness_invariants(problem, params) == problem._audit_readiness_invariants
    )
    details["services_and_ingresses_unchanged"] = unchanged
    ok = ok and traffic_ok and unchanged
    return ok, build_verify_result(
        fault_type=problem.root_cause_name,
        verified=ok,
        details={**details, "traffic": traffic},
    )
