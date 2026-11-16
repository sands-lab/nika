"""The benchmark engine owns release tasks: their IDs, rows, and trials (#292).

``tests/fixtures/benchmark/release_tasks.json`` comes from the current NIKA's
engine API v1 reference. For every task of each published release split it
records the task ID, the admission class, and the sha256 of the trial request
the task deployed. This engine must list the same tasks and deploy the same
trials.

The reference's task row also lists the task's failures as ``problems``. This
NIKA's benchmark runner never put that key in a release trial's row, and its
engine reads the failures from the request's ``failures``, so the row keeps
this NIKA's shape and the hash adds the key back.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from nika.benchmark_engine.engine import BenchmarkEngine
from nika.benchmark_engine.protocol import EngineError, TrialRequest
from nika.benchmark_engine.tasks import resolve_trial
from nika.workflows.benchmark.release import list_releases, load_release

FIXTURE = Path(__file__).parents[1] / "fixtures/benchmark/release_tasks.json"
PUBLISHED = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _deployed_sha256(request: TrialRequest) -> str:
    deployed = request.model_dump(
        mode="json",
        include={"task", "failures", "inject", "expected_root_causes", "healthy"},
    )
    deployed["task"]["problems"] = deployed["failures"]
    return hashlib.sha256(
        json.dumps(deployed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.contract
def test_every_published_release_split_is_pinned() -> None:
    assert sorted(PUBLISHED) == sorted(
        f"{version}/{split}"
        for version in list_releases()
        for split in load_release(version).splits
    )


@pytest.mark.contract
@pytest.mark.parametrize("release_split", sorted(PUBLISHED))
def test_published_release_tasks_keep_their_ids_and_trials(release_split) -> None:
    release, split = release_split.split("/")
    listed = BenchmarkEngine().list_tasks(release, split)
    expected = PUBLISHED[release_split]
    assert [task.task_id for task in listed.tasks] == [
        task["task_id"] for task in expected
    ]
    assert [task.resource_class for task in listed.tasks] == [
        task["resource_class"] for task in expected
    ]
    for task in expected:
        request = resolve_trial(
            TrialRequest(task_id=task["task_id"], release=release, split=split)
        )
        assert "problems" not in (request.task or {})
        assert _deployed_sha256(request) == task["deployed_sha256"], task["task_id"]


@pytest.mark.unit
def test_engine_refuses_unknown_task_ids_and_release_rows_before_deploying() -> None:
    engine = BenchmarkEngine()
    with patch("nika.benchmark_engine.engine.start_net_env") as deploy:
        with pytest.raises(EngineError, match="Unknown task id"):
            engine.start_trial(
                TrialRequest(task_id="no-such-task", release="0.2.0", split="test")
            )
        with pytest.raises(EngineError, match="start by task_id"):
            engine.start_trial(
                TrialRequest(
                    task={"scenario": "dc_clos", "problem": "healthy"},
                    release="0.2.0",
                )
            )
    deploy.assert_not_called()
