"""Docker E2E: deploy scenarios and run full behavioral verify via evaluate_scenario."""

from __future__ import annotations

import pytest

from nika.net_env.utils.routeros.common import IMAGE as ROUTEROS_IMAGE
from nika.net_env.utils.iosxr.common import IMAGE as XRD_IMAGE
from nika.runtime.factory import resolve_backend
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import (
    containerlab_prerequisites,
    docker_available,
    docker_image_available,
    linux_vrf_available,
    privileged_lab_supported,
)
from tests.support.scenario_e2e import ScenarioE2ECase, run_scenario_e2e

pytestmark = [pytest.mark.e2e, pytest.mark.nightly]

_VENDOR_IMAGES = {
    "iosxr_simple_bgp": XRD_IMAGE,
    "routeros_simple_bgp": ROUTEROS_IMAGE,
}
_PROFILE_IMAGES = {"iosxr": XRD_IMAGE, "routeros": ROUTEROS_IMAGE}


def _vendor_image(case: ScenarioE2ECase) -> str | None:
    args = list(case.env_run_args)
    if "--device-profile" in args:
        return _PROFILE_IMAGES.get(args[args.index("--device-profile") + 1])
    return _VENDOR_IMAGES.get(case.scenario)


def _case_id(case: ScenarioE2ECase) -> str:
    args = list(case.env_run_args)
    if "--device-profile" in args:
        return f"{case.scenario}-{args[args.index('--device-profile') + 1]}"
    return case.scenario


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
    ScenarioE2ECase(
        "routeros_simple_bgp",
        env_run_args=(),
        topo_size=None,
    ),
    *(
        ScenarioE2ECase(scenario, env_run_args=("-s", "s", "--device-profile", "iosxr"))
        for scenario in ("dc_clos", "campus_lan", "enterprise_branch")
    ),
)

_K8S_CASES = (
    ScenarioE2ECase("k8s_lab", env_run_args=(), topo_size=None),
    ScenarioE2ECase("llmd_lab", env_run_args=(), topo_size=None),
)

_CLAB_CASES = (
    ScenarioE2ECase(
        "min3clos", env_run_args=(), backend="containerlab", topo_size=None
    ),
)

_ISP_CASES = (
    ScenarioE2ECase(
        "isp_pdh",
        env_run_args=("--igp", "isis"),
        topo_size=None,
    ),
    ScenarioE2ECase(
        "isp_arpanet196912",
        env_run_args=(
            "--device-profile",
            "iosxr",
            "--igp",
            "isis",
            "--bgp-mode",
            "ibgp_rr",
        ),
        topo_size=None,
    ),
)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class KatharaScenarioE2ETest(IntegrationTestCase):
    @pytest.mark.parametrize("case", _KATHARA_CASES, ids=_case_id)
    def test_evaluate_scenario(self, case: ScenarioE2ECase) -> None:
        image = _vendor_image(case)
        if image and not docker_image_available(image):
            pytest.skip(f"Vendor image {image} not installed locally")
        if case.scenario == "enterprise_branch" and not linux_vrf_available():
            pytest.skip("Host kernel lacks Linux VRF (required by enterprise_branch)")
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


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class IspScenarioE2ETest(IntegrationTestCase):
    @pytest.mark.parametrize("case", _ISP_CASES, ids=_case_id)
    def test_evaluate_scenario(self, case: ScenarioE2ECase) -> None:
        image = _vendor_image(case)
        if image and not docker_image_available(image):
            pytest.skip(f"Vendor image {image} not installed locally")
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
