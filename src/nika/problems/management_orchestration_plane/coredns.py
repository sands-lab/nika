from __future__ import annotations

import json
import re
from typing import ClassVar

from nika.problems.base import FailureDomain
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import k8s_control_node
from nika.problems.support.kubernetes.base import (
    K8sParams,
    K8sProblemBase,
    control_node_from_net_env,
)
from nika.problems.support.oracle import OraclePlaybook, all_of, contains, shell


def forward_to_self(corefile: str) -> str:
    """Replace all root-zone upstreams while retaining the other plugins."""
    if not re.search(r"(?m)^[ \t]*loop[ \t]*(?:#.*)?$", corefile):
        raise ValueError("CoreDNS requires an enabled loop plugin.")
    updated, count = re.subn(
        r"(?m)^([ \t]*forward[ \t]+\.[ \t]+)[^{}\n#]*[^ \t{}\n#]",
        r"\g<1>127.0.0.1",
        corefile,
    )
    if count != 1:
        raise ValueError("CoreDNS requires one root-zone forward directive.")
    return updated


class CoreDNSForwardLoop(K8sProblemBase):
    """CoreDNS forwards root-zone queries to its own loopback listener.

    The loop plugin detects the startup HINFO forwarding loop and exits.
    Kubernetes restarts the process, leaving CoreDNS in CrashLoopBackOff.
    https://coredns.io/plugins/loop/#troubleshooting-loops-in-kubernetes-clusters
    https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/
    """

    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    root_cause_name = "k8s_coredns_forward_loop"
    description = "CoreDNS forwards DNS queries back to itself."
    symptom_probe = "custom"
    symptom_class = "unreachable"
    symptom_desc = "In-cluster application checks fail to resolve Service names."
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "coredns"]
    COMPATIBLE_COLUMNS = {"k8s_lab", "llmd_lab"}
    Params = K8sParams
    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"control_node": k8s_control_node(ctx)}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        control = control_node_from_net_env(ctx.net_env)
        if not control or (inject.get("control_node") or control) != control:
            raise ValueError(
                "control_node must name the Kubernetes control-plane device."
            )

    def root_cause_resources(self, params):
        return [node_resource(self.control_node(params))]

    def oracle_playbook(self, params: K8sParams) -> OraclePlaybook:
        control = self.control_node(params)
        return OraclePlaybook(
            finding="CoreDNS forwards root-zone queries to its own loopback listener. Its loop plugin detects the forwarding loop and terminates DNS service.",
            steps=[
                shell(
                    control,
                    "kubectl get configmap coredns -n kube-system -o jsonpath='{.data.Corefile}'",
                    "Read the active CoreDNS Corefile and check the root-zone upstream and loop detector.",
                    all_of(
                        contains(
                            r"^\s*forward\s+\.\s+127\.0\.0\.1\s*(?:\{|$)",
                            "Root-zone forwarding uses CoreDNS's own loopback listener.",
                        ),
                        contains(
                            r"^\s*loop\s*(?:#.*)?$", "The loop detector is enabled."
                        ),
                    ),
                    evidence=True,
                ),
                shell(
                    control,
                    "kubectl logs -n kube-system -l k8s-app=kube-dns --all-containers=true --prefix --tail=40 --previous; "
                    "kubectl logs -n kube-system -l k8s-app=kube-dns --all-containers=true --prefix --tail=40",
                    "Read current and previous CoreDNS logs for the loop detector's fatal diagnostic.",
                    contains(
                        r"plugin/loop:.*Loop .*detected",
                        "CoreDNS explicitly reports a forwarding loop, distinguishing this crash from DNS filtering, process termination, or unrelated CrashLoopBackOff.",
                    ),
                    evidence=True,
                    attempts=24,
                    interval_sec=5,
                    timeout=30,
                ),
            ],
        )

    def _corefile(self, params):
        return self.runtime.lab_api.kubectl_json(
            self.control_node(params), "get configmap coredns -n kube-system"
        )["data"]["Corefile"]

    def _apply_corefile(self, params, corefile):
        self.runtime.lab_api.kubectl_apply_manifest(
            self.control_node(params),
            json.dumps(
                {
                    "apiVersion": "v1",
                    "kind": "ConfigMap",
                    "metadata": {"name": "coredns", "namespace": "kube-system"},
                    "data": {**self._original_data, "Corefile": corefile},
                }
            ),
        )
        # Recreate every replica so the startup-only loop detector runs and
        # a rolling update cannot retain a healthy old CoreDNS replica.
        self.runtime.lab_api.kubectl(
            self.control_node(params),
            "delete pods -n kube-system -l k8s-app=kube-dns --wait=false",
        )

    def inject_fault(self, params):
        if self.control_node(params) != control_node_from_net_env(self.net_env):
            raise ValueError(
                "control_node must name the Kubernetes control-plane device."
            )
        data = self.runtime.lab_api.kubectl_json(
            self.control_node(params), "get configmap coredns -n kube-system"
        )["data"]
        original = data["Corefile"]
        injected = forward_to_self(original)
        self._original_data = data
        self._original_corefile = original
        self._injected_corefile = injected
        self._apply_corefile(params, injected)
        self.k8s_namespace = "kube-system"
        self.record_k8s_object("configmap", "coredns", namespace="kube-system")

    def verify_fault(self, params):
        def check():
            corefile = self._corefile(params)
            pods = self.runtime.lab_api.kubectl_json(
                self.control_node(params),
                "get pods -n kube-system -l k8s-app=kube-dns",
            ).get("items", [])
            active = [
                pod for pod in pods if not pod["metadata"].get("deletionTimestamp")
            ]
            crashed = sum(
                any(
                    cs.get("state", {}).get("waiting", {}).get("reason")
                    == "CrashLoopBackOff"
                    for cs in pod.get("status", {}).get("containerStatuses", [])
                )
                for pod in active
            )
            return (
                corefile == getattr(self, "_injected_corefile", None)
                and len(active) > 0
                and crashed == len(active)
            ), {
                "corefile": corefile,
                "coredns_pods": len(active),
                "coredns_crashloops": crashed,
            }

        return self.poll_verify(check)

    def recover_fault(self, params):
        from nika.validation.symptom.coredns_loop_probes import forward_loop_baseline

        original = getattr(self, "_original_corefile", None)
        if original is None:
            return {"verified": False, "details": {"error": "No saved Corefile."}}
        self._apply_corefile(params, original)

        def healthy():
            ok, details = forward_loop_baseline(self, params)
            restored = self._corefile(params) == original
            return restored and ok, {"corefile_restored": restored, **details}

        return self.poll_verify(healthy, timeout=300)
