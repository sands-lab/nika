"""Kubernetes filtering incidents restore service access and node readiness."""

import pytest

from nika.problems.support.polling import wait_until
from nika.validation.symptom.custom import evaluate_custom_baseline
from nika.validation.symptom import evaluate_symptom
from nika.validation.presence import bound_injected_problem
from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.workflows.failure.inject import inject_failure
from nika.workflows.benchmark.profile_validation import heavy_lab_lock
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available, privileged_lab_supported

pytestmark = [pytest.mark.e2e, pytest.mark.nightly]


@pytest.mark.skipif(
    not docker_available() or not privileged_lab_supported(),
    reason="privileged Docker lab required",
)
class TestKubernetesRecovery(IntegrationTestCase):
    def test_filtering_recovery_cycles(self):
        session_id = None
        with heavy_lab_lock():
            try:
                session_id = self._start_env("k8s_lab")
                self._assert_session_ready(session_id, "k8s_lab")
                for name in (
                    "k8s_clusterip_routing_broken",
                    "k8s_coredns_isolated",
                    "k8s_worker_apiserver_partition",
                ):
                    params = resolve_inject_params(name, "k8s_lab", "s", seed=42)
                    inject_failure(
                        [name], session_id=session_id, param_overrides=params
                    )
                    bound = bound_injected_problem(session_id)
                    assert bound is not None
                    symptom = None

                    def affected():
                        nonlocal symptom
                        symptom = evaluate_symptom(
                            bound[0].runtime,
                            name,
                            bound[1],
                            scenario="k8s_lab",
                            problem=bound[0],
                        )
                        return symptom[0] is True

                    assert wait_until(affected, 240, interval_s=5), (name, symptom)
                    result = bound[0].recover_fault(bound[1])
                    assert result["verified"], (name, result)
                    result = bound[0].recover_fault(bound[1])
                    assert result["verified"], (name, result)
                    health = None

                    def healthy():
                        nonlocal health
                        health = evaluate_custom_baseline(name, bound[0], bound[1])
                        return health is not None and health[0] is True

                    assert wait_until(healthy, 240, interval_s=5), (name, health)
            finally:
                if session_id is not None:
                    self._close_session(session_id)
