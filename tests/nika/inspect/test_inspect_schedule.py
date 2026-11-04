"""Scheduler artifacts exposed in Inspect and terminal progress."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from nika.inspect.adapters import adapt_nika_event
from nika.inspect.schedule import read_schedule_progress
from nika.inspect.server import create_inspect_app
from nika.schedule import Schedule
from nika.workflows.benchmark.display import BenchmarkProgress

pytestmark = pytest.mark.unit


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value))


def _journal(directory: Path, *rows: dict) -> None:
    with (directory / "schedule-events.jsonl").open("a") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


@pytest.fixture
def task(tmp_path: Path) -> Path:
    directory = tmp_path / "trial"
    directory.mkdir()
    _write(directory / "run.json", {"session_id": "trial", "status": "running"})
    plan = Schedule.model_validate(
        {
            "agent_start": 10,
            "events": [
                {
                    "problem": "link_down",
                    "inject": {"host_name": "r1", "intf_name": "eth0"},
                    "at": 2,
                    "duration": 3,
                    "repeat": {"every": 10, "count": 2},
                },
                {
                    "problem": "link_down",
                    "inject": {"host_name": "r2", "intf_name": "eth0"},
                    "at": 8,
                },
            ],
        }
    )
    (directory / "schedule.json").write_text(plan.model_dump_json())
    _journal(
        directory,
        {"status": "started", "timestamp": "2026-01-01T00:00:00+00:00", "elapsed": 0},
    )
    return directory


def test_inspect_schedule_live_and_finished(task: Path) -> None:
    _journal(
        task,
        {
            "status": "dispatched",
            "event_index": 0,
            "occurrence_index": 0,
            "operation": "start",
        },
        {"status": "applied", "event_index": 0, "occurrence_index": 0, "elapsed": 2.4},
        {"status": "verified", "event_index": 0, "occurrence_index": 0, "elapsed": 2.8},
        {
            "status": "finished",
            "event_index": 0,
            "occurrence_index": 0,
            "operation": "start",
        },
    )
    with TestClient(create_inspect_app(results_root=task.parent)) as client:
        response = client.get("/api/sessions/trial")
        assert response.status_code == 200
        progress = response.json()["schedule"]
        assert progress["state"] == "running"
        assert progress["active_occurrences"] == 1
        assert progress["completed_operations"] == 1
        assert progress["total_operations"] == 5
        assert progress["next_at"] == 5
        assert "r1" in progress["next_label"]
        first, other, repeat = progress["occurrences"]
        assert first["kind"] == "failure"
        assert progress["started_at"] == "2026-01-01T00:00:00+00:00"
        assert first["actual_start"] == 2.4 and first["verified_at"] == 2.8
        assert repeat["planned_start"] == 12 and repeat["occurrence_index"] == 1
        assert "r2" in other["label"] and other["planned_stop"] is None
        _journal(
            task,
            {
                "status": "recovered",
                "event_index": 0,
                "occurrence_index": 0,
                "elapsed": 5.3,
            },
            {
                "status": "applied",
                "event_index": 1,
                "occurrence_index": 0,
                "elapsed": 8.2,
            },
            {"status": "closed", "elapsed": 20},
        )
        _write(task / "schedule-status.json", {"completed": True})
        closed = client.get("/api/sessions/trial").json()["schedule"]
        assert closed["state"] == "complete" and closed["elapsed_sec"] == 20
        assert closed["active_occurrences"] == 0 and closed["next_at"] is None
        assert closed["occurrences"][0]["actual_stop"] == 5.3
        assert closed["occurrences"][1]["state"] == "ended"
        assert closed["occurrences"][1]["actual_stop"] is None


def test_schedule_failure_and_partial_artifacts(task: Path) -> None:
    _journal(
        task,
        {
            "status": "operation_failed",
            "event_index": 0,
            "occurrence_index": 0,
            "error": "recovery failed",
        },
        {"status": "failed", "error": "recovery failed", "elapsed": 6},
    )
    with (task / "schedule-events.jsonl").open("a") as handle:
        handle.write('{"status":')
    progress = read_schedule_progress(task)
    assert progress.state == "failed" and progress.error == "recovery failed"
    assert progress.occurrences[0].state == "failed"
    assert progress.next_label is None
    (task / "schedule.json").write_text('{"events":')
    assert read_schedule_progress(task) is None
    (task / "schedule.json").unlink()
    assert read_schedule_progress(task) is None


def test_schedule_event_and_terminal_stream(task: Path, capsys) -> None:
    event = {
        "status": "verified",
        "elapsed": 2.8,
        "planned_offset": 2,
        "lateness_sec": 0.8,
        "occurrence_index": 0,
        "summary": "link_down: host_name=r1, intf_name=eth0",
    }
    adapted = adapt_nika_event({"event": "schedule_event", "data": event}, index=0)
    assert adapted.title == "schedule: verified"
    assert "r1" in adapted.summary and "planned 2s" in adapted.summary
    with BenchmarkProgress(1, output_mode="agent") as progress:
        progress.start_trials(["first"])
        progress.attach_session("first", task)
        progress._poll_trajectories()
        _journal(task, event)
        progress._poll_trajectories()
        progress._poll_trajectories()
        progress.finish_trial("first")
    output = capsys.readouterr().out
    assert output.count("first | schedule +2.80s verified") == 1
    assert "late 0.80s" in output


def test_schedule_remote_clock(task: Path) -> None:
    _write(task / "run.json", {"remote_clock_offset_sec": 40})
    (task / "nika.jsonl").write_text(
        json.dumps({"event": "agent_start", "timestamp": "2026-01-01T00:00:50+00:00"})
        + "\n"
    )
    _journal(task, {"status": "closed", "elapsed": 20})
    progress = read_schedule_progress(task)
    assert progress.agent_actual_start == 10
    assert progress.started_at == "2026-01-01T00:00:40+00:00"
    assert progress.elapsed_sec == 20


def test_elapsed_keeps_advancing_after_events_finish_until_session_close(
    task, monkeypatch
):
    from datetime import UTC, datetime
    import nika.inspect.schedule as reader

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)

    monkeypatch.setattr(reader, "datetime", Clock)
    _write(task / "schedule-status.json", {"completed": True})
    _journal(task, {"status": "completed", "elapsed": 15})
    assert read_schedule_progress(task).elapsed_sec == 30
    _journal(task, {"status": "closed", "elapsed": 32})
    assert read_schedule_progress(task).elapsed_sec == 32
