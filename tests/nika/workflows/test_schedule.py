"""Contracts for scheduled task identity, validation, and final diagnosis."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from types import SimpleNamespace

import pytest

from nika.schedule import Schedule, normalize_schedule, schedule_for_task
from nika.workflows.benchmark.load_config import (
    load_benchmark_yaml,
    normalize_benchmark_row,
)
from nika.workflows.benchmark.resume import benchmark_row_fingerprint
from nika.workflows.benchmark.trials import case_key_for_row, has_valid_submission

pytestmark = pytest.mark.contract


@pytest.mark.parametrize(
    "problems", [["healthy"], ["link_down"], ["link_down", "dns_record_error"]]
)
def test_runtime_defaults_keep_authored_task_identity(problems):
    inject = {
        "link_down": {"host_name": "client_1_1", "intf_name": "eth0"},
        "dns_record_error": {
            "host_name": "web_1",
            "target_domain": "pod0",
            "target_website": "web0",
        },
    }
    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos",
            "topo_size": "s",
            "problems": problems,
            "inject": inject.get(problems[0], {}) if len(problems) == 1 else inject,
        }
    )
    original = deepcopy(row)
    identity = benchmark_row_fingerprint(row)
    plan = schedule_for_task(row)
    assert plan.agent_start is None
    assert plan.static
    assert [e.problem for e in plan.events] == [p for p in problems if p != "healthy"]
    assert all(
        e.at == 0 and e.duration is None and e.repeat is None for e in plan.events
    )
    assert row == original and "schedule" not in row
    assert benchmark_row_fingerprint(row) == identity


@pytest.mark.parametrize("agent_start, waits", [(None, True), (0, False)])
def test_default_agent_start_waits_for_verified_initial_events(
    tmp_path, monkeypatch, agent_start, waits
):
    from nika.workflows import schedule as runtime

    session = SimpleNamespace(session_dir=str(tmp_path))
    monkeypatch.setattr(
        runtime,
        "Session",
        lambda: SimpleNamespace(load_running_session=lambda **kw: session),
    )
    plan = Schedule(agent_start=agent_start, events=[{"problem": "link_down", "at": 0}])
    scheduler = runtime.SessionScheduler(f"defaults-{waits}", plan)
    applying, verified, released = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    records, errors = [], []
    monkeypatch.setattr(
        scheduler, "_record", lambda status, *args, **kw: records.append(status)
    )

    def inject(*args):
        applying.set()
        assert verified.wait(5)
        records.append("verified")

    def release():
        try:
            scheduler.wait_agent_start()
            released.set()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(scheduler, "_execute", inject)
    waiter = threading.Thread(target=release)
    scheduler.start()
    try:
        assert applying.wait(5)
        waiter.start()
        if waits:
            assert not released.wait(0.1)
        else:
            assert released.wait(5)
        verified.set()
        waiter.join(timeout=5)
        assert released.is_set() and not errors
        if waits:
            assert records.index("verified") < records.index("agent_released")
            assert scheduler.complete.is_set()
    finally:
        verified.set()
        waiter.join(timeout=5)
        scheduler.close()


def task():
    return {
        "scenario": "dc_clos",
        "topo_size": "s",
        "schedule": {
            "agent_start": "0s",
            "events": [
                {
                    "problem": "link_down",
                    "inject": {"host_name": "client_1_1", "intf_name": "eth0"},
                    "at": "1m",
                    "duration": "5s",
                    "repeat": {"every": "30s", "count": 2},
                },
                {
                    "problem": "link_down",
                    "inject": {"host_name": "web_1", "intf_name": "eth0"},
                    "at": "0s",
                    "duration": "10s",
                },
            ],
        },
    }


def test_repeated_failure_types_preserve_targets_and_identity(tmp_path):
    raw = task()
    row = normalize_benchmark_row(raw)
    assert row["problems"] == ["link_down"]
    assert [e["inject"]["host_name"] for e in row["schedule"]["events"]] == [
        "client_1_1",
        "web_1",
    ]
    equivalent = deepcopy(raw)
    equivalent["schedule"]["events"][0]["at"] = "60s"
    assert benchmark_row_fingerprint(row) == benchmark_row_fingerprint(
        normalize_benchmark_row(equivalent)
    )
    changed = deepcopy(raw)
    changed["schedule"]["events"][0]["at"] = "61s"
    other = normalize_benchmark_row(changed)
    assert benchmark_row_fingerprint(row) != benchmark_row_fingerprint(other)
    assert case_key_for_row(row) != case_key_for_row(other)
    path = tmp_path / "tasks.yaml"
    path.write_text(json.dumps({"cases": [raw]}))
    assert load_benchmark_yaml(path) == [row]


def test_time_sorting_keeps_authored_instance_indices():
    plan = normalize_schedule(task()["schedule"], "dc_clos")
    actions = plan.actions()
    assert [(a.at, a.event_index, a.occurrence_index, a.stopping) for a in actions] == [
        (0, 1, 0, False),
        (10, 1, 0, True),
        (60, 0, 0, False),
        (65, 0, 0, True),
        (90, 0, 1, False),
        (95, 0, 1, True),
    ]
    event = task()["schedule"]["events"][0]
    event.update(at="0s", duration="30s")
    actions = Schedule(agent_start=0, events=[event]).actions()
    assert actions[1].stopping and not actions[2].stopping
    assert actions[1].at == actions[2].at == 30


@pytest.mark.parametrize(
    "patch",
    [
        {"at": True},
        {"at": float("inf")},
        {"at": "-1s"},
        {"duration": "0s"},
        {"id": "manual"},
        {"repeat": {"every": "1s", "count": 2}},
        {"repeat": {"every": "10s"}},
        {"repeat": {"every": "10s", "count": True}},
        {"traffic": "background"},
    ],
)
def test_reject_ambiguous_or_unbounded_schedule(patch):
    raw = task()["schedule"]
    raw["events"][0].update(patch)
    with pytest.raises(ValueError):
        normalize_schedule(raw, "dc_clos")


def test_reject_mixed_failure_sources(tmp_path):
    raw = task()
    raw["problem"] = "link_down"
    path = tmp_path / "tasks.yaml"
    path.write_text(json.dumps({"cases": [raw]}))
    with pytest.raises(ValueError, match="inside schedule.events"):
        load_benchmark_yaml(path)


def test_dns_recovery_allows_timed_repetition():
    raw = {
        "agent_start": 0,
        "events": [
            {
                "problem": "dns_record_error",
                "inject": {
                    "host_name": "web_1",
                    "target_domain": "pod0",
                    "target_website": "web0",
                },
                "at": 0,
                "duration": 1,
                "repeat": {"every": 5, "count": 2},
            }
        ],
    }
    plan = normalize_schedule(raw, "dc_clos")
    assert [action.at for action in plan.actions()] == [0, 1, 5, 6]


def test_reject_inherited_recovery_placeholder(monkeypatch):
    from nika.problems.base import ProblemBase
    from nika.problems.registry import get_problem_class

    monkeypatch.setattr(
        get_problem_class("link_down"), "recover_fault", ProblemBase.recover_fault
    )
    with pytest.raises(ValueError, match="scheduled recovery"):
        normalize_schedule(task()["schedule"], "dc_clos")


@pytest.mark.parametrize("fail_recovery", [False, True])
def test_cleanup_restores_snapshots_in_reverse_injection_order(
    tmp_path, monkeypatch, fail_recovery
):
    from nika.workflows import schedule as runtime

    session = SimpleNamespace(session_dir=str(tmp_path))
    monkeypatch.setattr(
        runtime,
        "Session",
        lambda: SimpleNamespace(load_running_session=lambda **kw: session),
    )
    scheduler = runtime.SessionScheduler("cleanup-order", Schedule())
    monkeypatch.setattr(scheduler, "_record", lambda *args, **kw: None)
    state = {"value": "healthy"}
    saved, restored = {}, []
    for key in [(2, 0), (0, 0), (1, 0)]:
        saved[key] = state["value"]
        state["value"] = key
        scheduler._active[key] = None

    def stop(key):
        restored.append(key)
        if fail_recovery and key == (0, 0):
            raise RuntimeError("Restore failed")
        state["value"] = saved[key]
        scheduler._active.pop(key)

    monkeypatch.setattr(scheduler, "_stop", stop)
    if fail_recovery:
        with pytest.raises(runtime.ScheduleError, match="Restore failed"):
            scheduler.close()
        assert restored == [(1, 0), (0, 0)]
        assert state["value"] == (0, 0)
        assert list(scheduler._active) == [(2, 0), (0, 0)]
    else:
        scheduler.close()
        assert restored == [(1, 0), (0, 0), (2, 0)]
        assert state["value"] == "healthy"
        assert not scheduler._active


def test_incomplete_or_failed_timeline_cannot_score_a_submission(tmp_path):
    (tmp_path / "submission.json").write_text("{}")
    assert has_valid_submission(tmp_path)
    path = tmp_path / "schedule-status.json"
    for state in ({"completed": False}, {"completed": True, "error": "inject failed"}):
        path.write_text(json.dumps(state))
        assert not has_valid_submission(tmp_path)
    path.write_text(json.dumps({"completed": True, "error": None}))
    assert has_valid_submission(tmp_path)


@pytest.mark.parametrize("finish_during_diagnosis", [False, True])
def test_early_report_continues_observation_before_freezing(
    monkeypatch, finish_during_diagnosis
):
    from nika.agents.registry import run_pipeline

    ready = threading.Event()
    state = SimpleNamespace(complete=ready, revision=0)
    state.observation_revision = lambda: state.revision
    state.raise_if_failed = lambda: None

    def progress(*args):
        state.revision += 1
        ready.set()

    state.wait_for_progress = progress
    monkeypatch.setattr("nika.workflows.schedule.scheduler_for", lambda _: state)
    frozen = []
    monkeypatch.setattr(
        "nika.agents.mcp_config.begin_submission_mcp_phase",
        lambda sid, report: frozen.append(report) or {},
    )
    prompts = []

    async def diagnose(prompt):
        prompts.append(prompt)
        if finish_during_diagnosis and len(prompts) == 1:
            progress()
        return f"report {len(prompts)}"

    async def submit(report, context):
        assert report == "report 2"

    agent = SimpleNamespace(
        session_id="unit-session", run_diagnosis=diagnose, run_submission=submit
    )
    asyncio.run(run_pipeline(agent, "Inspect this network"))
    assert len(prompts) == 2
    assert "report 1" in prompts[1]
    assert frozen == ["report 2"]


def test_materialized_schedule_preserves_identity_and_per_event_truth(tmp_path):
    from nika.workflows.benchmark.migrate import materialize_case, write_cases_yaml

    raw = task()
    materialized = materialize_case(raw)
    assert "problem" not in materialized and "inject" not in materialized
    assert len(materialized["root_causes"]) == 2
    assert all(event["root_causes"] for event in materialized["schedule"]["events"])
    path = tmp_path / "release.yaml"
    write_cases_yaml(path, seed=42, cases=[materialized])
    loaded = load_benchmark_yaml(path)[0]
    assert benchmark_row_fingerprint(loaded) == benchmark_row_fingerprint(
        normalize_benchmark_row(raw)
    )


@pytest.mark.parametrize("finish_during_diagnosis", [False, True])
@pytest.mark.parametrize("exhaust_final_budget", [False, True])
def test_sandbox_keeps_diagnosis_phase_and_budget_during_continuation(
    monkeypatch, tmp_path, finish_during_diagnosis, exhaust_final_budget
):
    from agent.runtime.contract import REPORT_FILENAME, TASK_FILENAME
    from nika.agents.sandbox.manager import SandboxManager, SandboxSession

    ready = threading.Event()
    state = SimpleNamespace(complete=ready, revision=0)
    state.observation_revision = lambda: state.revision
    state.raise_if_failed = lambda: None

    def progress(*args):
        state.revision += 1
        ready.set()

    state.wait_for_progress = progress
    monkeypatch.setattr("nika.workflows.schedule.scheduler_for", lambda _: state)
    frozen = []
    monkeypatch.setattr(
        "nika.agents.mcp_config.begin_submission_mcp_phase",
        lambda sid, report: frozen.append(report) or {},
    )
    (tmp_path / TASK_FILENAME).write_text(
        json.dumps({"task_description": "Inspect the network"})
    )
    manager = SandboxManager.__new__(SandboxManager)
    manager.backend = SimpleNamespace(
        pull_files=lambda *args: None, push_files=lambda *args: None
    )
    phases, prompts = [], []

    def run_step(**kwargs):
        phases.append(kwargs["phase"])
        if finish_during_diagnosis and len(phases) == 1:
            progress()
        prompts.append(
            json.loads((tmp_path / TASK_FILENAME).read_text())["task_description"]
        )
        (tmp_path / REPORT_FILENAME).write_text(
            json.dumps({"report": f"report {len(phases)}"})
        )
        return exhaust_final_budget and len(phases) == 2

    manager._run_step = run_step
    manager._budget_report = lambda *args, **kwargs: "budget report"
    manager._map_report_on_host = lambda *args, **kwargs: None
    phase_changes = []
    calls = SimpleNamespace(set_phase=phase_changes.append)
    sandbox = SandboxSession(
        "unit-sandbox", tmp_path, 0, agent_session_id="opaque", call_log=calls
    )
    manager._run_in_sandbox(
        sandbox_session=sandbox,
        stream_output=False,
        timeout_sec=10,
        session=SimpleNamespace(
            session_id="unit",
            session_dir=str(tmp_path),
            task_description="Inspect the network",
        ),
        report_mapper=object(),
    )
    assert phases == ["diagnosis", "diagnosis"]
    assert "report 1" in prompts[1]
    assert phase_changes == ["submission"]
    assert frozen == ["budget report" if exhaust_final_budget else "report 2"]


def test_cancel_before_start_records_no_elapsed_time(tmp_path, monkeypatch):
    from nika.workflows import schedule as runtime

    session = SimpleNamespace(session_dir=str(tmp_path))
    monkeypatch.setattr(
        runtime,
        "Session",
        lambda: SimpleNamespace(load_running_session=lambda **kw: session),
    )
    scheduler = runtime.SessionScheduler(
        "cancel-before-start", Schedule(events=[{"problem": "link_down", "at": 10}])
    )
    scheduler.close()
    rows = [
        json.loads(line)
        for line in (tmp_path / "schedule-events.jsonl").read_text().splitlines()
    ]
    cancelled = [row for row in rows if row["status"] == "cancelled"]
    assert len(cancelled) == 1
    assert cancelled[0]["planned_offset"] == 10
    assert cancelled[0]["elapsed"] is None
    assert cancelled[0]["lateness_sec"] is None
    assert rows[-1]["status"] == "closed" and not rows[-1]["cleanup_errors"]


@pytest.mark.parametrize("repeat", [None, {"every": 10, "count": 2}])
def test_schedule_rejects_faults_that_finish_before_agent_dispatch(repeat):
    event = {"problem": "link_down", "at": 0, "duration": 5}
    if repeat:
        event["repeat"] = repeat
    with pytest.raises(ValueError, match="observable"):
        Schedule(agent_start=20, events=[event])
    assert Schedule(agent_start=0, events=[event])
