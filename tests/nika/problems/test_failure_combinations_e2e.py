"""Live test of every tested multi-fault task in the catalog."""

from __future__ import annotations

import pytest

from nika.problems.combinations import list_combinations
from tests.support.combination_e2e import run_combination_e2e
from nika.validation.symptom.combinations import CHECKS
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.e2e

TASKS = [
    pytest.param(
        combination,
        row,
        id="-".join(
            [combination.name, row["scenario"], row.get("device_profile", "default")]
        ),
    )
    for combination in list_combinations()
    for row in combination.rows()
]


def test_every_combination_has_a_live_check():
    assert set(CHECKS) == {combination.name for combination in list_combinations()}


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestFailureCombinations(IntegrationTestCase):
    @pytest.mark.parametrize(("combination", "row"), TASKS)
    def test_task_holds_on_a_live_network(self, combination, row) -> None:
        run_combination_e2e(self, combination, row)
