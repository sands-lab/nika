"""Contract tests for running nika-bench 0.1.0 on current NIKA (no Docker)."""

from __future__ import annotations

import pytest

from nika.net_env.compat.v010 import scenario_specs
from nika.problems.compat.v010 import FAILURES, SCENARIO_FAILURE_OVERRIDES
from nika.net_env.net_env_pool import list_all_net_envs
from nika.problems.rca.materialize import (
    assert_root_causes_match,
    ground_truth_for_case,
)
from nika.problems.registry import get_problem_class, list_avail_problem_names
from nika.workflows.benchmark.inject_resolve import validate_benchmark_case
from nika.workflows.benchmark.release import (
    DEPRECATED_RELEASES,
    load_release,
    preflight_release,
)

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("split", ["dev", "test"])
def test_0_1_0_split_loads_and_passes_preflight(split: str) -> None:
    release = load_release("nika@0.1", split=split)
    assert release.version == "0.1.0"
    assert "0.1.0" not in DEPRECATED_RELEASES
    assert len(release.cases) == 56
    preflight_release(release, check_images=False)


@pytest.mark.parametrize("split", ["dev", "test"])
def test_0_1_0_rows_validate_and_match_ground_truth(split: str) -> None:
    for row in load_release("0.1.0", split=split).cases:
        topo_size = row.get("topo_size") or ""
        validate_benchmark_case(
            row["scenario"], row["problem"], row["inject"], topo_size
        )
        truth = ground_truth_for_case(
            problem=row["problem"],
            params=row["inject"],
            scenario=row["scenario"],
            topo_size=topo_size,
        )
        assert_root_causes_match(truth, row["root_causes"])


def test_legacy_code_stays_out_of_current_catalogs() -> None:
    legacy_scenarios = set(scenario_specs())
    assert not legacy_scenarios & set(list_all_net_envs())
    for split in ("dev", "test"):
        cases = load_release("0.2.0", split=split).cases
        assert not legacy_scenarios & {row["scenario"] for row in cases}
    assert not set(FAILURES) & set(list_avail_problem_names())


def test_legacy_overrides_apply_only_on_original_labs() -> None:
    for (scenario, name), cls in SCENARIO_FAILURE_OVERRIDES.items():
        assert get_problem_class(name, scenario) is cls
        assert get_problem_class(name) is not cls
