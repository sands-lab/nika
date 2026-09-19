"""Kathara light-startup smoke: env run (default depth) then close."""

from __future__ import annotations

import os

import pytest

from tests.ci.constants import CI_KATHARA_STARTUP_SCENARIOS
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


def _selected_scenarios() -> list[str]:
    selected = os.environ.get("NIKA_CI_SCENARIO", "").strip()
    if selected:
        if selected not in CI_KATHARA_STARTUP_SCENARIOS:
            raise ValueError(
                f"Unknown NIKA_CI_SCENARIO={selected!r}; "
                f"expected one of {CI_KATHARA_STARTUP_SCENARIOS}"
            )
        return [selected]
    return list(CI_KATHARA_STARTUP_SCENARIOS)


@pytest.mark.parametrize("scenario", _selected_scenarios())
def test_kathara_scenario_light_startup(scenario: str) -> None:
    """Deploy size=s; start_net_env already polls light startup_verify_lab."""
    helper = IntegrationTestCase()
    session_id = helper._start_env(scenario, ["-s", "s"])
    try:
        helper._assert_session_ready(session_id, scenario)
    finally:
        helper._close_session(session_id)
