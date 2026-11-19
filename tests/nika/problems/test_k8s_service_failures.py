"""Compatibility of Kubernetes Service failures with k8s_lab and llmd_lab."""

from __future__ import annotations

import pytest

from nika.problems.registry import compatible

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("problem", "k8s_lab", "llmd_lab"),
    [
        ("lb_advertisement_missing", True, True),
        ("k8s_service_target_port_mismatch", True, True),
        ("k8s_service_selector_mismatch", True, True),
        ("k8s_ingress_class_mismatch", True, False),
        ("k8s_ingress_rate_limit_misconfig", True, False),
        ("k8s_deployment_replicas_zero", True, True),
        ("k8s_node_noexecute_taint", True, False),
    ],
)
def test_k8s_service_failure_lab_coverage(
    problem: str, k8s_lab: bool, llmd_lab: bool
) -> None:
    assert compatible(problem, "k8s_lab") is k8s_lab
    assert compatible(problem, "llmd_lab") is llmd_lab


@pytest.mark.parametrize(
    ("problem", "owner"),
    [
        ("k8s_service_selector_mismatch", "node"),
        ("k8s_service_target_port_mismatch", "node"),
        ("k8s_deployment_replicas_zero", "node"),
        ("k8s_readiness_probe_misconfig", "node"),
        ("lb_advertisement_missing", "node"),
        ("k8s_ingress_class_mismatch", "node"),
        ("k8s_worker_apiserver_partition", "node"),
        ("k8s_networkpolicy_deny", "k8s"),
    ],
)
def test_public_kubernetes_owner_guidance_is_precise(problem, owner):
    from nika.problems.ownership import ownership_entries

    assert ownership_entries([problem])[0]["owner_kind"] == owner
