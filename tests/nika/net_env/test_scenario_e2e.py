"""Docker E2E: deploy scenarios and run full behavioral verify via evaluate_scenario."""

from __future__ import annotations

import pytest

from nika.runtime.factory import resolve_backend
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_available,
    privileged_lab_supported,
)
from tests.support.scenario_e2e import ScenarioE2ECase, run_scenario_e2e


def _iosxr_image_available() -> bool:
    from nika.net_env.kathara.interdomain_routing.iosxr_simple_bgp.lab import IMAGE
    from nika.net_env.utils.kathara.docker_files.docker_images import image_exists

    return image_exists(IMAGE)

_KATHARA_CASES = tuple(
    ScenarioE2ECase(scenario, env_run_args=("-s", "s"))
    for scenario in (
        "dc_clos",
        "campus_lan",
        "enterprise_branch",
        "sdn_l3_clos",
        "p4_dc_fabric",
        "p4_dc_gateway",
    )
) + (
    ScenarioE2ECase(
        "iosxr_simple_bgp",
        env_run_args=(),
        topo_size=None,
    ),
)

_K8S_CASES = (
    ScenarioE2ECase("k8s_lab", env_run_args=(), topo_size=None),
    ScenarioE2ECase("llmd_lab", env_run_args=(), topo_size=None),
)

_CLAB_CASES = (ScenarioE2ECase("min3clos", env_run_args=(), backend="containerlab", topo_size=None),)

_ISP_CASES = (
    ScenarioE2ECase(
        "isp_pdh",
        env_run_args=("--igp", "isis"),
        topo_size=None,
    ),
)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaScenarioE2ETest(IntegrationTestCase):
    @pytest.mark.parametrize("case", _KATHARA_CASES, ids=lambda c: c.scenario)
    def test_evaluate_scenario(self, case: ScenarioE2ECase) -> None:
        if case.scenario == "iosxr_simple_bgp" and not _iosxr_image_available():
            pytest.skip("XRd Control Plane image not installed locally")
        session_id = self._start_env(case.scenario, list(case.env_run_args))
        try:
            row = self._assert_session_ready(session_id, case.scenario)
            kwargs = self._scenario_kwargs(session_id)
            run_scenario_e2e(
                case,
                session_id=session_id,
                scenario_kwargs={
                    **kwargs,
                    "backend": resolve_backend(row),
                },
            )
        finally:
            self._close_session(session_id)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
@pytest.mark.skipif(
    not privileged_lab_supported(),
    reason="Privileged k3s containers require Docker access",
)
class K8sScenarioE2ETest(IntegrationTestCase):
    @pytest.mark.parametrize("case", _K8S_CASES, ids=lambda c: c.scenario)
    def test_evaluate_scenario(self, case: ScenarioE2ECase) -> None:
        session_id = self._start_env(case.scenario, list(case.env_run_args))
        try:
            row = self._assert_session_ready(session_id, case.scenario)
            kwargs = self._scenario_kwargs(session_id)
            run_scenario_e2e(
                case,
                session_id=session_id,
                scenario_kwargs={
                    **kwargs,
                    "backend": resolve_backend(row),
                },
            )
        finally:
            self._close_session(session_id)


@pytest.mark.skipif(
    not containerlab_prerequisites(),
    reason="containerlab, gnmic, or Docker not available",
)
class Min3ClosScenarioE2ETest(IntegrationTestCase):
    @pytest.mark.parametrize("case", _CLAB_CASES, ids=lambda c: c.scenario)
    def test_evaluate_scenario(self, case: ScenarioE2ECase) -> None:
        session_id = self._start_env(case.scenario, list(case.env_run_args))
        try:
            row = self._assert_session_ready(session_id, case.scenario)
            kwargs = self._scenario_kwargs(session_id)
            run_scenario_e2e(
                case,
                session_id=session_id,
                scenario_kwargs={
                    **kwargs,
                    "backend": resolve_backend(row),
                },
            )
        finally:
            self._close_session(session_id)


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class IspScenarioE2ETest(IntegrationTestCase):
    @pytest.mark.parametrize("case", _ISP_CASES, ids=lambda c: c.scenario)
    def test_evaluate_scenario(self, case: ScenarioE2ECase) -> None:
        session_id = self._start_env(case.scenario, list(case.env_run_args))
        try:
            row = self._assert_session_ready(session_id, case.scenario)
            kwargs = self._scenario_kwargs(session_id)
            run_scenario_e2e(
                case,
                session_id=session_id,
                scenario_kwargs={
                    **kwargs,
                    "backend": resolve_backend(row),
                },
            )
        finally:
            self._close_session(session_id)
