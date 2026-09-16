"""Curated failure-inject smoke (ground truth only)."""

from __future__ import annotations

import os

import pytest

from nika.mcp.session_context import SESSION_ID_ENV
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.ci.constants import CI_FAILURE_INJECT_CASES
from tests.support.failure_contract import inject_and_assert_ground_truth
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


def _cases() -> list[tuple[str, str, dict[str, str]]]:
    selected = os.environ.get("NIKA_CI_FAILURE_CASE", "").strip()
    if not selected:
        return list(CI_FAILURE_INJECT_CASES)
    for scenario, problem, params in CI_FAILURE_INJECT_CASES:
        case_id = f"{scenario}-{problem}"
        if case_id == selected:
            return [(scenario, problem, params)]
    raise ValueError(
        f"Unknown NIKA_CI_FAILURE_CASE={selected!r}; "
        f"expected scenario-problem id from CI_FAILURE_INJECT_CASES"
    )


_SELECTED_CASES = _cases()


@pytest.mark.parametrize(
    ("scenario", "problem", "inject_params"),
    _SELECTED_CASES,
    ids=[f"{s}-{p}" for s, p, _ in _SELECTED_CASES],
)
def test_failure_inject_smoke(
    scenario: str, problem: str, inject_params: dict[str, str]
) -> None:
    topo_size = "s" if scenario != "simple_bgp" else None
    session_id = start_net_env(
        scenario,
        topo_size,
        session_tag=resolve_session_tag(context="test"),
    )
    prev = os.environ.get(SESSION_ID_ENV)
    os.environ[SESSION_ID_ENV] = session_id
    try:
        inject_and_assert_ground_truth(session_id, scenario, problem, inject_params)
    finally:
        close_session(session_id=session_id)
        if prev is None:
            os.environ.pop(SESSION_ID_ENV, None)
        else:
            os.environ[SESSION_ID_ENV] = prev
