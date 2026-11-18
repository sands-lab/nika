"""The benchmark engine owns release tasks: their IDs, rows, and trials (#292).

``tests/fixtures/benchmark/release_tasks.json`` was computed once from the
frontend code that built release trials before the engine owned them. For
every task of each published release split it records the task ID, the
admission class, and the sha256 of the trial request the task deployed. A
newly published release adds its entries, computed by its engine.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

import pytest

from nika.benchmark_engine.engine import BenchmarkEngine
from nika.benchmark_engine.protocol import EngineError, TrialRequest
from nika.benchmark_engine.tasks import resolve_trial
from nika.workflows.benchmark.release import ReleaseError, list_releases, load_release
from nika.workflows.benchmark.run import run_benchmark_from_release

FIXTURE = Path(__file__).parents[1] / "fixtures/benchmark/release_tasks.json"
PUBLISHED = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _deployed_sha256(request: TrialRequest) -> str:
    deployed = request.model_dump(
        mode="json",
        include={"task", "failures", "inject", "expected_root_causes", "healthy"},
    )
    return hashlib.sha256(
        json.dumps(deployed, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.contract
def test_every_published_release_split_is_pinned() -> None:
    assert sorted(PUBLISHED) == sorted(
        f"{version}/{split}"
        for version in list_releases()
        for split in load_release(version, read_tasks=False).splits
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


@pytest.mark.unit
def test_frontend_refuses_a_task_list_from_another_split_file(tmp_path) -> None:
    class _Engine(BenchmarkEngine):
        def list_tasks(self, release, split):
            listed = super().list_tasks(release, split)
            return listed.model_copy(update={"task_file_sha256": "0" * 64})

    with (
        patch(
            "nika.workflows.benchmark.run.release_engine",
            lambda *args, **kwargs: nullcontext(),
        ),
        patch("nika.workflows.benchmark.run.benchmark_engine", _Engine),
        patch("nika.workflows.benchmark.run.run_benchmark_trials") as runner,
    ):
        with pytest.raises(ReleaseError, match="does not match the sha256"):
            run_benchmark_from_release(
                "0.2.0",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                split="test",
                result_dir=str(tmp_path / "run"),
                check_images=False,
            )
    runner.assert_not_called()
