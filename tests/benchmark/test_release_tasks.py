"""Release 0.1.0's task IDs and the trials they deploy do not change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika.benchmark_engine.engine import BenchmarkEngine
from nika.benchmark_engine.protocol import EngineError, TrialRequest
from nika.benchmark_engine.tasks import release_task_rows, resolve_trial
from nika.workflows.benchmark.release import load_release
from nika.workflows.benchmark.resume import benchmark_row_fingerprint
from nika.workflows.benchmark.trials import case_key_for_row, expand_trials

FIXTURE = Path(__file__).parents[1] / "fixtures" / "benchmark" / "release_tasks.json"
PINNED = json.loads(FIXTURE.read_text(encoding="utf-8"))["0.1.0"]


@pytest.mark.parametrize("split", ["dev", "test"])
def test_list_tasks_matches_the_pinned_task_ids(split: str) -> None:
    listed = BenchmarkEngine().list_tasks("0.1.0", split)
    pinned = PINNED[split]
    assert listed.task_file_sha256 == pinned["task_file_sha256"]
    assert [
        (task.task_id, task.resource_class, task.faults) for task in listed.tasks
    ] == [
        (task["task_id"], task["resource_class"], task["faults"])
        for task in pinned["tasks"]
    ]


@pytest.mark.parametrize("split", ["dev", "test"])
def test_task_ids_are_the_release_runners_case_keys(split: str) -> None:
    """Trial directories keep the names 0.1.0's own runner gave them."""
    rows = load_release("0.1.0", split=split).cases
    trials = expand_trials(rows, 1)
    listed = BenchmarkEngine().list_tasks("0.1.0", split).tasks
    assert [task.task_id for task in listed] == [trial.case_key for trial in trials]


@pytest.mark.parametrize("split", ["dev", "test"])
def test_task_id_resolves_to_the_runners_trial(split: str) -> None:
    pinned = {t["task_id"]: t["row_fingerprint"] for t in PINNED[split]["tasks"]}
    for task_id, row in release_task_rows("0.1.0", split).items():
        request = resolve_trial(
            TrialRequest(
                task_id=task_id,
                release="0.1.0",
                split=split,
                session_id=f"{task_id}__t01",
                trial_dir=f"/results/trials/{task_id}__t01",
            )
        )
        assert benchmark_row_fingerprint(request.task) == pinned[task_id]
        assert case_key_for_row(request.task) == task_id
        assert request.failures == [row["problem"]]
        assert request.inject == row["inject"]
        assert request.expected_root_causes == row["root_causes"]


def test_unknown_task_id_is_refused_before_deploy(monkeypatch) -> None:
    from nika.workflows.env import start

    monkeypatch.setattr(start, "start_net_env", lambda *a, **k: pytest.fail("deployed"))
    with pytest.raises(EngineError, match="Unknown task id"):
        BenchmarkEngine().start_trial(
            TrialRequest(
                task_id="dc_clos_bgp__link_down__00000000",
                release="0.1.0",
                split="test",
                session_id="s1",
                trial_dir="/results/trials/s1",
            )
        )


def test_engine_serves_only_release_010() -> None:
    with pytest.raises(EngineError, match="serves release 0.1.0"):
        BenchmarkEngine().describe("0.2.0")
    described = BenchmarkEngine().describe("0.1.0")
    assert described.api_versions == [1]
    assert described.task_file_sha256 == {
        f"{split}.yaml": PINNED[split]["task_file_sha256"] for split in ("dev", "test")
    }


def test_unsupported_gateway_options_are_not_agent_failures() -> None:
    """A refused configuration must not count as a zero-scored agent failure."""
    from nika.benchmark_engine.protocol import GatewayRequest, TrialHandle

    handle = TrialHandle(session_id="s1", trial_dir="/results/trials/s1")
    with pytest.raises(EngineError) as refused:
        BenchmarkEngine().agent_endpoints(
            handle, GatewayRequest(tool_call_limits={"diagnosis": 50})
        )
    assert refused.value.outcome == "infra_failed"
