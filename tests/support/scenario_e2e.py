"""Shared harness for scenario deploy → evaluate_scenario E2E."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from nika.net_env.net_env_pool import get_net_env_instance
from tests.support.integration_base import TEST_SESSION_ID_RE
from tests.support.scenario_evaluate import evaluate_scenario


@dataclass(frozen=True)
class ScenarioE2ECase:
    scenario: str
    env_run_args: tuple[str, ...] = ()
    topo_size: str | None = "s"
    backend: str | None = None


def _assert_test_session_id(session_id: str) -> None:
    assert re.search(
        TEST_SESSION_ID_RE,
        session_id,
    ), f"refusing cleanup: session_id must match test tag pattern: {session_id!r}"


def run_scenario_e2e(
    case: ScenarioE2ECase,
    *,
    session_id: str,
    scenario_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Run full ``verify_lab`` behavioral checks on an active session."""
    _assert_test_session_id(session_id)
    kwargs = dict(scenario_kwargs)
    if case.backend is not None:
        kwargs["backend"] = case.backend
    if case.topo_size is not None:
        kwargs.setdefault("topo_size", case.topo_size)
    net_env = get_net_env_instance(case.scenario, **kwargs)
    ok, result = evaluate_scenario(net_env)
    assert ok is True, result
    return result
