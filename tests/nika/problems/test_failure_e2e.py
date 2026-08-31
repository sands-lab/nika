"""Parametrized failure inject → verify → symptom → recover E2E tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.support.failure_e2e import FailureE2ECase, run_failure_e2e
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_available,
    privileged_lab_supported,
)


@dataclass(frozen=True)
class _FlapScenarioCase:
    id: str
    scenario: str
    env_args: tuple[str, ...]
    topo_size: str = "s"
    isp_options: dict[str, str] | None = None
    require_clab: bool = False
    require_privileged: bool = False


FLAP_SCENARIOS = (
    _FlapScenarioCase("simple_bgp", "simple_bgp", (), ""),
    _FlapScenarioCase("dc_clos", "dc_clos", ("-s", "s")),
    _FlapScenarioCase("campus_lan", "campus_lan", ("-s", "s")),
    _FlapScenarioCase("enterprise_branch", "enterprise_branch", ("-s", "s")),
    _FlapScenarioCase("k8s_lab", "k8s_lab", (), "", require_privileged=True),
    _FlapScenarioCase("min3clos", "min3clos", (), "", require_clab=True),
    _FlapScenarioCase("p4_dc_fabric", "p4_dc_fabric", ("-s", "s")),
    _FlapScenarioCase("p4_dc_gateway", "p4_dc_gateway", ("-s", "s")),
    _FlapScenarioCase("sdn_l3_clos", "sdn_l3_clos", ("-s", "s")),
    _FlapScenarioCase(
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
    _FlapScenarioCase(
        "isp-abilene-ebgp-rpki",
        "isp_abilene_ebgp_rpki",
        (),
        "s",
        isp_options=None,
    ),
)
FLAP_SEEDS = (0, 1, 4, 7)
FLAP_BY_SCENARIO = {flap.scenario: flap for flap in FLAP_SCENARIOS}


def _flap_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="link_flap",
            scenario=flap.scenario,
            env_run_args=flap.env_args,
            topo_size=flap.topo_size or "s",
            inject_seed=seed,
            isp_options=flap.isp_options,
        )
        for flap in FLAP_SCENARIOS
        for seed in FLAP_SEEDS
    ]


def _link_detach_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="link_detach",
            scenario=scenario,
            env_run_args=("-s", "s"),
            inject_seed=0,
        )
        for scenario in ("dc_clos", "enterprise_branch", "sdn_l3_clos")
    ]


def _incast_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="incast_traffic_network_limitation",
            scenario=scenario,
            env_run_args=("-s", "s"),
            inject_seed=1,
        )
        for scenario in ("dc_clos", "sdn_l3_clos", "p4_dc_gateway")
    ]


def _lb_conn_exhaustion_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="lb_connection_state_exhaustion",
            scenario="p4_dc_gateway",
            env_run_args=("-s", "s"),
            inject_seed=seed,
            checks=frozenset({"verify", "symptom"}),
            sleep_after_inject_sec=2.0,
        )
        for seed in (0, 1, 4, 7)
    ]


CORE_E2E_CASES = (
    FailureE2ECase(
        problem="link_down",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="link_capacity_bottleneck",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="load_balancer_overload",
        scenario="campus_lan",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="sender_resource_contention",
        scenario="dc_clos",
        env_run_args=("-s", "s"),
        inject_seed=1,
    ),
    FailureE2ECase(
        problem="tcp_receive_window_limited",
        scenario="enterprise_branch",
        env_run_args=("-s", "m"),
        topo_size="m",
        inject_seed=1,
        param_overrides={"baseline_trials": "1", "iperf_duration_sec": "3"},
    ),
    FailureE2ECase(
        problem="bmv2_switch_down",
        scenario="p4_dc_fabric",
        env_run_args=("-s", "s"),
        inject_seed=0,
        checks=frozenset({"verify", "symptom"}),
        sleep_after_inject_sec=1.0,
    ),
    FailureE2ECase(
        problem="host_static_blackhole",
        scenario="min3clos",
        env_run_args=(),
        topo_size="",
        inject_params={"host_name": "leaf1"},
        # StaticBlackHole has no recover_fault; assert verify + dataplane symptom.
        checks=frozenset({"verify", "symptom"}),
        sleep_after_inject_sec=2.0,
    ),
)


def _pipeline_mismatch_e2e_cases() -> list[FailureE2ECase]:
    return [
        FailureE2ECase(
            problem="p4runtime_pipeline_mismatch",
            scenario="p4_dc_fabric",
            env_run_args=("-s", "s"),
            inject_seed=seed,
            checks=frozenset({"verify", "symptom"}),
            sleep_after_inject_sec=2.0,
        )
        for seed in (0, 1, 4)
    ]


def _all_e2e_cases() -> list[FailureE2ECase]:
    return [
        *CORE_E2E_CASES,
        *_link_detach_e2e_cases(),
        *_incast_e2e_cases(),
        *_lb_conn_exhaustion_e2e_cases(),
        *_pipeline_mismatch_e2e_cases(),
        *_flap_e2e_cases(),
    ]


def _case_id(case: FailureE2ECase) -> str:
    parts = [case.problem, case.scenario]
    if case.inject_seed != 1:
        parts.append(f"seed{case.inject_seed}")
    if case.topo_size not in ("", "s"):
        parts.append(f"topo-{case.topo_size}")
    return "-".join(parts)


def _skip_reason(case: FailureE2ECase) -> str | None:
    if not docker_available():
        return "docker required"
    if case.scenario == "min3clos" and not containerlab_prerequisites():
        return "containerlab/gnmic not available"
    if case.problem != "link_flap":
        return None
    flap = FLAP_BY_SCENARIO.get(case.scenario)
    if flap is None:
        return None
    if flap.require_clab and not containerlab_prerequisites():
        return "containerlab/gnmic not available"
    if flap.require_privileged and not privileged_lab_supported():
        return "privileged k8s lab not supported"
    return None


@pytest.mark.integration
@pytest.mark.parametrize("case", _all_e2e_cases(), ids=_case_id)
class TestFailureE2E(IntegrationTestCase):
    def test_inject_verify_symptom_recover(self, case: FailureE2ECase) -> None:
        skip = _skip_reason(case)
        if skip:
            pytest.skip(skip)

        session_id = None
        try:
            session_id = self._start_env(case.scenario, list(case.env_run_args))
            self._assert_session_ready(session_id, case.scenario)
            run_failure_e2e(
                case,
                scenario_kwargs=self._scenario_kwargs(session_id),
            )
        finally:
            if session_id is not None:
                self._close_session(session_id)
