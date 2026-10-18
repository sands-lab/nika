"""Docker E2E for device_forwarding_packet_corruption (TC BPF gray TCP degradation)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.problems.registry import get_problem_class, list_avail_problem_names
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from nika.validation.symptom import evaluate_symptom, get_symptom_contract
from nika.validation.symptom.custom import (
    evaluate_custom_baseline,
    evaluate_custom_symptom,
)


PROBLEM = "device_forwarding_packet_corruption"
SEEDS = (1,)


@dataclass(frozen=True)
class ForwardingCorruptionCase:
    id: str
    scenario: str
    env_args: tuple[str, ...] = ("-s", "s")
    topo_size: str = "s"


CORRUPTION_CASES = (
    ForwardingCorruptionCase("dc_clos", "dc_clos"),
    ForwardingCorruptionCase("campus_lan", "campus_lan"),
    ForwardingCorruptionCase("enterprise_branch", "enterprise_branch"),
    ForwardingCorruptionCase("sdn_l3_clos", "sdn_l3_clos"),
)


@pytest.mark.contract
def test_failure_registers_and_contracts() -> None:
    assert PROBLEM in list_avail_problem_names()
    cls = get_problem_class(PROBLEM)
    assert cls is not None
    contract = get_symptom_contract(PROBLEM)
    assert contract.symptom_class == "gray"
    assert contract.probe == "custom"
    assert "evaluate_symptom" not in cls.__dict__


def _skip_reason() -> str | None:
    if not docker_available():
        return "docker required"
    return None


@pytest.mark.e2e
@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("case", CORRUPTION_CASES, ids=[c.id for c in CORRUPTION_CASES])
class TestDeviceForwardingPacketCorruptionE2E(IntegrationTestCase):
    """Inject → verify_fault → gray TCP symptom → recover."""

    def test_inject_verify_symptom_recover(
        self, case: ForwardingCorruptionCase, seed: int
    ) -> None:
        skip = _skip_reason()
        if skip:
            pytest.skip(skip)

        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(case.scenario, list(case.env_args))
            self._assert_session_ready(session_id, case.scenario)

            raw = resolve_inject_params(
                PROBLEM,
                case.scenario,
                case.topo_size,
                seed=seed,
            )
            cls = get_problem_class(PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(raw)
            runtime = problem.runtime

            baseline_ok, baseline = evaluate_custom_baseline(PROBLEM, problem, parsed)
            assert baseline_ok is True, baseline

            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify

                ok, symptom = evaluate_symptom(
                    runtime,
                    PROBLEM,
                    parsed,
                    scenario=case.scenario,
                    topo_size=case.topo_size,
                    problem=problem,
                )
                assert ok is True, symptom

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
                still, after = evaluate_custom_symptom(PROBLEM, problem, parsed)
                assert still is False, after
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
