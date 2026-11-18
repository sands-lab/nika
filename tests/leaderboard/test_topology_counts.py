import json

import pytest

from nika.workflows.leaderboard.trial_results import trial_result_from_dir

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("counts", [None, {"node_count": 18, "link_count": 20}])
def test_packed_trial_preserves_topology_counts(tmp_path, counts):
    run = {"outcome": "agent_failed", "score_status": "no_submission"}
    if counts is not None:
        run["metadata"] = {"topology_counts": counts}
    (tmp_path / "run.json").write_text(json.dumps(run))
    trial = trial_result_from_dir(
        trial_id="task__t01",
        case_key="task",
        trial_index=1,
        scenario="dc_clos",
        problem="healthy",
        session_dir=tmp_path,
    )
    result = json.loads(trial.model_dump_json())
    assert result["node_count"] == (18 if counts else None)
    assert result["link_count"] == (20 if counts else None)


def test_frozen_engine_inventory_counts_isolated_nodes_and_excludes_telemetry(tmp_path):
    run = {
        "outcome": "agent_failed",
        "score_status": "no_submission",
        "metadata": {
            "machine_identities": {
                name: {"role": "host"}
                for name in ("a", "b", "isolated", "collector", "mgmt")
            },
            "telemetry": {"collector": "collector", "switch": "mgmt"},
        },
    }
    (tmp_path / "run.json").write_text(json.dumps(run))
    trial = trial_result_from_dir(
        trial_id="task__t01",
        case_key="task",
        trial_index=1,
        scenario="test",
        problem="healthy",
        session_dir=tmp_path,
    )
    assert trial.node_count == 3
    assert trial.link_count is None
