"""Unit and integration tests for load_balancer_overload."""

from __future__ import annotations

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.problems.ownership import owner_kind_for_fault
from nika.problems.registry import get_problem_class, list_avail_problem_names
from tests.support.symptom import evaluate_symptom, get_symptom_contract
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


def test_failure_registers_and_contracts() -> None:
    assert "load_balancer_overload" in list_avail_problem_names()
    cls = get_problem_class("load_balancer_overload")
    assert cls is not None
    assert owner_kind_for_fault("load_balancer_overload") == "node_or_k8s"
    contract = get_symptom_contract("load_balancer_overload")
    assert contract.symptom_class == "degradation"
    assert contract.probe == "custom"
    assert "load balancer" in (cls.symptom_desc or "").lower()
    assert cls.COMPATIBLE_COLUMNS == frozenset({"campus_lan"})
    assert "load_balancer" in cls.TAGS
    assert "http" in cls.TAGS
    assert "evaluate_symptom" not in cls.__dict__


def test_inject_params_target_campus_lan_vip() -> None:
    params = resolve_inject_params("load_balancer_overload", "campus_lan", "s", seed=1)
    assert params["host_name"] == "load_balancer"
    assert params["client_host"].startswith("pc_")
    assert params["vip_url"].endswith("/small")
    assert "web99.local" in params["vip_url"]
    assert "web0.local" in params["control_url"]
    assert "20.200.0.2" in params["backend_url"]
    assert float(params["cpu_quota"]) > 0
    assert int(params["concurrency"]) >= 1
    assert int(params["load_workers"]) >= 1


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestLoadBalancerOverloadCampusLan(IntegrationTestCase):
    """CPU pin + background VIP load: nginx saturates, control/backend stay healthy."""

    SCENARIO = "campus_lan"
    PROBLEM = "load_balancer_overload"

    def test_inject_behavior_restore_cleanup(self) -> None:
        from nika.problems.support.cpu_quota_helpers import read_nano_cpus

        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(self.SCENARIO, ["-s", "s"])
            self._assert_session_ready(session_id, self.SCENARIO)

            params = resolve_inject_params(self.PROBLEM, self.SCENARIO, "s", seed=1)
            cls = get_problem_class(self.PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(params)
            runtime = problem.runtime

            original_nano = read_nano_cpus(runtime, parsed.host_name)
            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify
                artifact = verify["details"]
                assert artifact["load_running"] is True
                assert artifact["capacity_ok"] is True
                assert artifact["nginx_running"] is True

                ok, symptom = evaluate_symptom(
                    runtime,
                    self.PROBLEM,
                    parsed,
                    scenario=self.SCENARIO,
                    problem=problem,
                )
                assert ok is True, symptom
                details = symptom["details"]
                assert details["nginx_saturated"] is True
                assert details["vip_degraded"] is True
                assert details["control_ok"] is True
                assert details["backend_ok_gate"] is True
                assert details["path_ok"] is True

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
                assert recovered["details"]["load_gone"] is True
                assert read_nano_cpus(runtime, parsed.host_name) == original_nano
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
