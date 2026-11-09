"""CoreDNS startup-loop and existing in-cluster workload observations."""

from __future__ import annotations

import shlex
import time
from typing import Any

from nika.problems.base import build_verify_result
from nika.problems.management_orchestration_plane.kubernetes import _SYSTEM_NAMESPACES
from nika.service.lab.k8s_api import K8sCommandError


def _ready(obj):
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in obj.get("status", {}).get("conditions", [])
    )


def _sample(problem, params):
    k8s = problem.runtime.lab_api
    control = problem.control_node(params)
    nodes = k8s.kubectl_json(control, "get nodes").get("items", [])
    pods = k8s.kubectl_json(control, "get pods -A").get("items", [])
    dns = [
        pod
        for pod in pods
        if pod["metadata"]["namespace"] == "kube-system"
        and pod["metadata"].get("labels", {}).get("k8s-app") == "kube-dns"
        and not pod["metadata"].get("deletionTimestamp")
    ]
    agents = [
        pod
        for pod in pods
        if pod["metadata"]["namespace"] == "monitoring"
        and pod["metadata"].get("labels", {}).get("app") == "uptime-agent"
    ]
    # Workloads include gateway and application Pods, and exclude completed Jobs.
    applications = [
        pod
        for pod in pods
        if pod["metadata"]["namespace"] not in _SYSTEM_NAMESPACES
        and pod.get("status", {}).get("phase") != "Succeeded"
        and not pod["metadata"].get("deletionTimestamp")
    ]
    sample = {
        "nodes": len(nodes),
        "nodes_ready": sum(_ready(node) for node in nodes),
        "application_pods": len(applications),
        "application_pods_ready": sum(_ready(pod) for pod in applications),
        "monitoring_pods": len(agents),
        "monitoring_pods_ready": sum(_ready(pod) for pod in agents),
        "coredns_pods": len(dns),
        "coredns_pods_ready": sum(_ready(pod) for pod in dns),
        "coredns_crashloops": sum(
            any(
                cs.get("state", {}).get("waiting", {}).get("reason")
                == "CrashLoopBackOff"
                for cs in pod.get("status", {}).get("containerStatuses", [])
            )
            for pod in dns
        ),
        "coredns_restarts": sum(
            cs.get("restartCount", 0)
            for pod in dns
            for cs in pod.get("status", {}).get("containerStatuses", [])
        ),
    }
    if not agents:
        return {**sample, "error": "No existing in-cluster application checks."}
    observer = min(agents, key=lambda pod: pod["metadata"]["name"])["metadata"]["name"]
    command = (
        'printf "last_success_age_sec=%s\\n" "$(($(date +%s) - $(cat /tmp/last-ok)))"; '
        "timeout 10 nslookup kubernetes.default.svc.cluster.local"
    )
    lookup = k8s.kubectl(
        control,
        f"exec -n monitoring {shlex.quote(observer)} -- sh -c {shlex.quote(command)}",
        timeout=20,
        check=False,
    )
    age_line = next(
        (
            line
            for line in lookup.stdout.splitlines()
            if line.startswith("last_success_age_sec=")
        ),
        "",
    )
    if not age_line or not age_line.partition("=")[2].isdigit():
        raise K8sCommandError(f"Unreadable monitoring check timestamp: {lookup}")
    sample.update(
        observer=observer,
        last_success_age_sec=int(age_line.partition("=")[2]),
        dns_ok=lookup.ok,
        dns_output=lookup.stdout + lookup.stderr,
    )
    return sample


def _controls_ok(sample):
    return (
        sample["nodes"] > 0
        and sample["nodes_ready"] == sample["nodes"]
        and sample["application_pods"] > 0
        and sample["application_pods_ready"] == sample["application_pods"]
    )


def forward_loop_baseline(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    try:
        sample = _sample(problem, params)
    except K8sCommandError as exc:
        return False, {"error": str(exc)}
    return (
        "error" not in sample
        and _controls_ok(sample)
        and sample["coredns_pods"] > 0
        and sample["coredns_pods_ready"] == sample["coredns_pods"]
        and sample["monitoring_pods"] > 0
        and sample["monitoring_pods_ready"] == sample["monitoring_pods"]
        and sample["dns_ok"]
        and sample["last_success_age_sec"] < 60
    ), sample


def forward_loop_broken(problem: Any, params: Any) -> tuple[bool, dict[str, Any]]:
    deadline = time.monotonic() + 240
    while True:
        try:
            sample = _sample(problem, params)
            k8s = problem.runtime.lab_api
            control = problem.control_node(params)
            dns_logs = k8s.kubectl(
                control,
                "logs -n kube-system -l k8s-app=kube-dns --previous --tail=10 --prefix",
                check=False,
            )
            agent_logs = k8s.kubectl(
                control,
                "logs -n monitoring -l app=uptime-agent --tail=4 --prefix",
                check=False,
            )
            sample.update(
                coredns_logs=dns_logs.stdout,
                monitoring_logs=agent_logs.stdout,
                loop_detected="plugin/loop: Loop" in dns_logs.stdout
                and "detected" in dns_logs.stdout,
                workload_dns_errors="bad address" in agent_logs.stdout,
            )
            verified = (
                "error" not in sample
                and _controls_ok(sample)
                and sample["coredns_pods"] > 0
                and sample["coredns_crashloops"] == sample["coredns_pods"]
                and sample["coredns_pods_ready"] == 0
                and sample["coredns_restarts"] > 0
                and sample["monitoring_pods"] > 0
                and sample["monitoring_pods_ready"] == 0
                and not sample["dns_ok"]
                and sample["last_success_age_sec"] >= 60
                and sample["loop_detected"]
                and sample["workload_dns_errors"]
            )
        except K8sCommandError as exc:
            verified, sample = False, {"error": str(exc)}
        if verified or time.monotonic() >= deadline:
            return verified, build_verify_result(
                fault_type=problem.root_cause_name, verified=verified, details=sample
            )
        time.sleep(5)
