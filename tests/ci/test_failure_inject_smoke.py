"""Curated failure-inject smoke on published scenarios (ground truth only)."""

from __future__ import annotations

import os

import pytest

from nika.mcp.session_context import SESSION_ID_ENV
from nika.utils.session_id import resolve_session_tag
from nika.workflows.env.start import start_net_env
from nika.workflows.session.close import close_session
from tests.ci.constants import CI_FAILURE_INJECT_CASES
from tests.support.failure_contract import (
    inject_and_assert_ground_truth,
    resolve_inject_params,
)
from tests.support.prerequisites import containerlab_prerequisites, docker_available

pytestmark = [
    pytest.mark.ci_smoke,
]


def _cases() -> list[tuple[str, str, str, str | None, str]]:
    selected = os.environ.get("NIKA_CI_FAILURE_CASE", "").strip()
    if not selected:
        return list(CI_FAILURE_INJECT_CASES)
    for case in CI_FAILURE_INJECT_CASES:
        if case[0] == selected:
            return [case]
    known = tuple(case_id for case_id, *_ in CI_FAILURE_INJECT_CASES)
    raise ValueError(
        f"Unknown NIKA_CI_FAILURE_CASE={selected!r}; expected one of {known}"
    )


_SELECTED_CASES = _cases()


@pytest.mark.parametrize(
    ("case_id", "scenario", "problem", "topo_size", "backend"),
    _SELECTED_CASES,
    ids=[case_id for case_id, *_ in _SELECTED_CASES],
)
def test_failure_inject_smoke(
    case_id: str,
    scenario: str,
    problem: str,
    topo_size: str | None,
    backend: str,
) -> None:
    del case_id  # used only as pytest id / matrix key
    if backend == "containerlab":
        if not containerlab_prerequisites():
            pytest.skip("containerlab (clab) + gnmic required")
    elif not docker_available():
        pytest.skip("Docker not available")

    inject_params = resolve_inject_params(
        scenario, problem, topo_size=topo_size or ""
    )
    session_id = start_net_env(
        scenario,
        topo_size,
        session_tag=resolve_session_tag(context="test"),
        backend=backend,
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
