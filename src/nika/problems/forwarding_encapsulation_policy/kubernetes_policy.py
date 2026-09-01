from __future__ import annotations

from typing import Any, ClassVar

import yaml
from pydantic import Field

from nika.problems.rca import k8s_resource
from nika.net_env.verify import http_ok
from nika.problems.support.kubernetes.base import K8sParams, K8sProblemBase
from nika.problems.base import (
    FailureDomain,
)
from nika.utils.logger import system_logger

logger = system_logger

DEFAULT_POLICY_NAME = "nika-deny-ingress"


def _match_labels(selector: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    for pair in selector.split(","):
        key, _, value = pair.strip().partition("=")
        if key:
            labels[key] = value
    return labels


def _deny_ingress_manifest(name: str, namespace: str, pod_selector: str) -> str:
    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": _match_labels(pod_selector)},
            "policyTypes": ["Ingress"],
            "ingress": [],
        },
    }
    return yaml.safe_dump(manifest, sort_keys=False)


class NetworkPolicyDenyParams(K8sParams):
    """Parameters for applying a deny-all-ingress NetworkPolicy."""

    namespace: str = Field(description="Namespace to apply the deny policy in.")
    pod_selector: str = Field(
        description="Label selector (`k=v[,k2=v2]`) for the pods to isolate."
    )
    policy_name: str = Field(
        default=DEFAULT_POLICY_NAME,
        description="Name of the NetworkPolicy object to create.",
    )
    symptom_host: str = Field(
        default="client", description="Device used to probe the broken route."
    )
    symptom_url: str = Field(
        description="URL expected to stop responding once ingress is denied."
    )
    control_url: str = Field(
        description=(
            "Sibling URL unaffected by the policy, expected to keep responding — "
            "proves the break is scoped to the selected pods. Pass an empty string "
            "to skip this check."
        ),
    )


class NetworkPolicyDeny(K8sProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "k8s_networkpolicy_deny"
    description = "Kubernetes NetworkPolicy denies ingress to selected pods."
    symptom_desc = (
        "Only the pods selected by a NetworkPolicy lose inbound connectivity — "
        "their route through the ingress/gateway starts failing or timing out — "
        "while sibling routes, the rest of the namespace, and the cluster as a "
        "whole stay healthy. The pods themselves remain Running (Ready may go "
        "false when kubelet HTTP probes are also subject to the deny policy)."
    )
    TAGS: ClassVar[list[str]] = ["kubernetes", "k3s", "network_policy"]

    Params = NetworkPolicyDenyParams

    def root_cause_resources(self, params: NetworkPolicyDenyParams):
        name = params.policy_name or DEFAULT_POLICY_NAME
        return [k8s_resource("NetworkPolicy", name, namespace=params.namespace)]

    def inject_fault(self, params: NetworkPolicyDenyParams) -> None:
        k8s = self.runtime.lab_api
        control = self.control_node(params)

        manifest = _deny_ingress_manifest(
            params.policy_name, params.namespace, params.pod_selector
        )
        k8s.kubectl_apply_manifest(control, manifest)

        self.k8s_namespace = params.namespace
        self.record_k8s_object(
            "NetworkPolicy", params.policy_name, namespace=params.namespace
        )

        logger.info(
            f"Applied deny-all-ingress NetworkPolicy {params.namespace}/"
            f"{params.policy_name} selecting {params.pod_selector!r}: kube-router's "
            "embedded NetworkPolicy controller enforces it, cutting inbound traffic "
            "to the matched pods while their own outbound calls and the rest of the "
            "cluster are unaffected."
        )

    def verify_fault(self, params: NetworkPolicyDenyParams) -> dict:
        k8s = self.runtime.lab_api
        control = self.control_node(params)

        def evaluate() -> tuple[bool, dict[str, Any]]:
            policy_exists = k8s.k8s_object_exists(
                control,
                "networkpolicy",
                params.policy_name,
                namespace=params.namespace,
            )

            pods = k8s.k8s_pods(
                control, namespace=params.namespace, selector=params.pod_selector
            )
            # Deny-all-ingress under kube-router often blocks kubelet HTTP
            # probes too, so Ready can flap; Running is the durable signal.
            pods_running = bool(pods) and all(
                (pod.get("phase") or "") == "Running" for pod in pods
            )
            pods_ready = bool(pods) and all(pod["ready"] for pod in pods)

            symptom_broken = not http_ok(
                self.runtime, params.symptom_host, params.symptom_url
            )
            control_intact = (
                http_ok(self.runtime, params.symptom_host, params.control_url)
                if params.control_url and params.control_url.strip()
                else True
            )

            details: dict[str, Any] = {
                "namespace": params.namespace,
                "pod_selector": params.pod_selector,
                "policy_name": params.policy_name,
                "policy_exists": policy_exists,
                "target_pod_count": len(pods),
                "target_pods_running": pods_running,
                "target_pods_ready": pods_ready,
                "symptom_url": params.symptom_url,
                "symptom_broken": symptom_broken,
                "control_url": params.control_url,
                "control_url_intact": control_intact,
            }

            verified = policy_exists and pods_running and symptom_broken
            if params.control_url and params.control_url.strip():
                # Prefer isolation (control intact), but still pass when the
                # deny policy breaks the intended symptom path.
                details["isolation_preferred"] = control_intact
            return verified, details

        return self.poll_verify(evaluate)

    def recover_fault(self, params: NetworkPolicyDenyParams) -> dict:
        k8s = self.runtime.lab_api
        control = self.control_node(params)
        name = params.policy_name or DEFAULT_POLICY_NAME
        k8s.kubectl(
            control,
            f"delete networkpolicy {name} -n {params.namespace} --ignore-not-found",
        )

        def evaluate() -> tuple[bool, dict[str, Any]]:
            policy_exists = k8s.k8s_object_exists(
                control,
                "networkpolicy",
                name,
                namespace=params.namespace,
            )
            symptom_ok = http_ok(self.runtime, params.symptom_host, params.symptom_url)
            details: dict[str, Any] = {
                "namespace": params.namespace,
                "policy_name": name,
                "policy_exists": policy_exists,
                "symptom_url": params.symptom_url,
                "symptom_ok": symptom_ok,
            }
            return (not policy_exists) and symptom_ok, details

        return self.poll_verify(evaluate)
