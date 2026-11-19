"""Release tasks retain their deployment and target contracts as sizes expand."""

from unittest.mock import patch

import pytest

from nika.net_env.net_env_pool import get_net_env_instance
from nika.problems.combinations import task_placement
from nika.workflows.benchmark.multi_fault import row_problems
from nika.workflows.benchmark.release import load_release, preflight_release
from nika.workflows.benchmark.task_label import format_task_label, parse_task_label

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("split", ["dev", "test"])
def test_release_tasks_keep_valid_targets_and_labels(split):
    release = load_release("0.3.0", split=split)
    preflight_release(release, check_images=False)
    for row in release.cases:
        if row["scenario"] not in {"k8s_lab", "llmd_lab"}:
            continue
        for problem in row_problems(row):
            label = format_task_label(row["scenario"], problem, row.get("topo_size"))
            assert parse_task_label(label) == (
                row["scenario"],
                row.get("topo_size") or "",
                problem,
            )
        with patch(
            "Kathara.manager.Kathara.Kathara.get_instance", return_value=object()
        ):
            env = get_net_env_instance(row["scenario"], topo_size=row.get("topo_size"))
        assert env.topo_size == row["topo_size"]
        placements = (
            task_placement(row).values()
            if len(row_problems(row)) > 1
            else [row.get("inject") or {}]
        )
        for inject in placements:
            for key in ("host_name", "host_name_2", "node_name", "control_node"):
                target = inject.get(key)
                if target:
                    assert target in env.lab.machines, row
