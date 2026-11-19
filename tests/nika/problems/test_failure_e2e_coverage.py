"""Every registered failure has an end-to-end inject, symptom, and recover case."""

from __future__ import annotations

import pytest

from nika.problems.registry import list_avail_problem_instances
from tests.nika.problems.test_failure_e2e import _all_e2e_cases

pytestmark = pytest.mark.contract


def test_every_registered_failure_has_an_e2e_case() -> None:
    covered = {case.problem for case in _all_e2e_cases()}
    assert {
        name
        for name, cls in list_avail_problem_instances().items()
        if not cls.combination_only
    } <= covered
