"""Kathara scenario smoke (published scenarios, size=s).

Default: full ``evaluate_scenario``. With ``NIKA_CI_VERIFY_DEPTH=artifact``,
session ready after light startup is enough (same path as PR startup smoke).
"""

from __future__ import annotations

import os

import pytest

from nika.runtime.factory import resolve_backend
from tests.ci.constants import CI_KATHARA_VERIFY_SCENARIOS
from tests.support.ci_depth import artifact_verify_only
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.scenario_e2e import ScenarioE2ECase, run_scenario_e2e

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


def _selected_scenarios() -> list[str]:
    selected = os.environ.get("NIKA_CI_VERIFY_SCENARIO", "").strip()
    if selected:
        if selected not in CI_KATHARA_VERIFY_SCENARIOS:
            raise ValueError(
                f"Unknown NIKA_CI_VERIFY_SCENARIO={selected!r}; "
                f"expected one of {CI_KATHARA_VERIFY_SCENARIOS}"
            )
        return [selected]
    return list(CI_KATHARA_VERIFY_SCENARIOS)


@pytest.mark.parametrize("scenario", _selected_scenarios())
def test_kathara_scenario_verify(scenario: str) -> None:
    """Deploy size=s; artifact mode stops at session ready, else evaluate_scenario."""
    helper = IntegrationTestCase()
    session_id = helper._start_env(scenario, ["-s", "s"])
    try:
        row = helper._assert_session_ready(session_id, scenario)
        if artifact_verify_only():
            return
        case = ScenarioE2ECase(scenario, env_run_args=("-s", "s"))
        run_scenario_e2e(
            case,
            session_id=session_id,
            scenario_kwargs={
                **helper._scenario_kwargs(session_id),
                "backend": resolve_backend(row),
            },
        )
    finally:
        helper._close_session(session_id)
