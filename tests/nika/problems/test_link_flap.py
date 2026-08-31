"""Docker E2E for link_flap (artifact verify + periodic flap symptom)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.problems.registry import get_problem_class, list_avail_problem_names
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_available,
    privileged_lab_supported,
)
from tests.support.symptom import evaluate_symptom, get_symptom_contract
from tests.support.symptom.flap_probes import assert_baseline_healthy
from tests.support.symptom.probe import _resolve_path

PROBLEM = "link_flap"
SEEDS = (0, 1, 4, 7)
K8S_SEEDS = (0,)


@dataclass(frozen=True)
class FlapScenarioCase:
    id: str
    scenario: str
    env_args: tuple[str, ...]
    topo_size: str = "s"
    isp_options: dict[str, str] | None = None
    require_clab: bool = False
    require_privileged: bool = False


FLAP_CASES = (
    FlapScenarioCase("simple_bgp", "simple_bgp", (), ""),
    FlapScenarioCase("dc_clos", "dc_clos", ("-s", "s")),
    FlapScenarioCase("campus_lan", "campus_lan", ("-s", "s")),
    FlapScenarioCase("enterprise_branch", "enterprise_branch", ("-s", "s")),
    FlapScenarioCase("k8s_lab", "k8s_lab", (), "", require_privileged=True),
    FlapScenarioCase("min3clos", "min3clos", (), "", require_clab=True),
    FlapScenarioCase("p4_dc_fabric", "p4_dc_fabric", ("-s", "s")),
    FlapScenarioCase("p4_dc_gateway", "p4_dc_gateway", ("-s", "s")),
    FlapScenarioCase("sdn_l3_clos", "sdn_l3_clos", ("-s", "s")),
    FlapScenarioCase(
        "isp-abilene-ebgp",
        "isp_abilene",
        ("--igp", "ospf", "--bgp-mode", "ebgp"),
        "s",
        isp_options={
            "igp": "ospf",
            "bgp_mode": "ebgp",
            "rpki": False,
        },
    ),
    FlapScenarioCase(
        "isp-abilene-ebgp-rpki",
        "isp_abilene_ebgp_rpki",
        (),
        "s",
        isp_options=None,
    ),
)


def test_failure_registers_and_contracts() -> None:
    assert PROBLEM in list_avail_problem_names()
    cls = get_problem_class(PROBLEM)
    assert cls is not None
    contract = get_symptom_contract(PROBLEM)
    assert contract.symptom_class == "loss"
    assert contract.probe == "custom"
    assert "evaluate_symptom" not in cls.__dict__


def _skip_reason(case: FlapScenarioCase) -> str | None:
    if not docker_available():
        return "docker required"
    if case.require_clab and not containerlab_prerequisites():
        return "containerlab/gnmic not available"
    if case.require_privileged and not privileged_lab_supported():
        return "privileged k8s lab not supported"
    return None


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("case", FLAP_CASES, ids=[c.id for c in FLAP_CASES])
class TestLinkFlapE2E(IntegrationTestCase):
    """Inject → verify_fault → periodic flap symptom → recover."""

    def test_inject_verify_symptom_recover(
        self, case: FlapScenarioCase, seed: int
    ) -> None:
        if case.scenario == "k8s_lab" and seed not in K8S_SEEDS:
            pytest.skip("k8s_lab uses a single seed to reduce privileged lab churn")
        skip = _skip_reason(case)
        if skip:
            pytest.skip(skip)

        session_id = None
        problem = None
        parsed = None
        try:
            try:
                session_id = self._start_env(case.scenario, list(case.env_args))
                self._assert_session_ready(session_id, case.scenario)
            except RuntimeError as exc:
                if case.scenario == "k8s_lab":
                    pytest.skip(f"k8s_lab unavailable: {exc}")
                raise

            raw = resolve_inject_params(
                PROBLEM,
                case.scenario,
                case.topo_size,
                seed=seed,
                isp_options=case.isp_options,
            )
            cls = get_problem_class(PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(raw)
            runtime = problem.runtime

            path = _resolve_path(case.scenario, parsed, topo_size=case.topo_size or "s")
            assert path is not None and path.dst_ip

            baseline_ok, baseline = assert_baseline_healthy(runtime, path)
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
                    topo_size=case.topo_size or "s",
                    problem=problem,
                )
                assert ok is True, symptom
                assert symptom.get("comparison", {}).get("periodic_loss") is True, (
                    symptom
                )

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
