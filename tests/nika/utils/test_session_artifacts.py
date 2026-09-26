"""Unit tests for session result-directory helpers."""

from __future__ import annotations

import json
from pathlib import Path

from nika.utils.session_artifacts import (
    is_job_run_dir,
    iter_session_dirs,
    last_session_error,
    order_run_json,
)


def _write_events(session_dir: Path, records: list[dict | str]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
    (session_dir / "nika.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_last_session_error_returns_most_recent_error(tmp_path: Path) -> None:
    _write_events(
        tmp_path,
        [
            {"level": "ERROR", "event": "env_start_failed", "message": "first boom"},
            {"level": "INFO", "event": "env_stop", "message": "stopped"},
            {"level": "ERROR", "event": "inject_failed", "message": "second boom"},
            {"level": "INFO", "event": "session_cleared", "message": "cleared"},
        ],
    )
    assert last_session_error(tmp_path) == "second boom"


def test_last_session_error_skips_malformed_and_empty_lines(tmp_path: Path) -> None:
    _write_events(
        tmp_path,
        [
            {"level": "ERROR", "event": "env_start_failed", "message": "real cause"},
            "",
            "{not json",
            {"level": "ERROR", "event": "noop", "message": "   "},
        ],
    )
    assert last_session_error(tmp_path) == "real cause"


def test_last_session_error_without_errors_or_log(tmp_path: Path) -> None:
    assert last_session_error(tmp_path / "missing") is None
    _write_events(tmp_path, [{"level": "INFO", "event": "env_start", "message": "ok"}])
    assert last_session_error(tmp_path) is None


def test_is_job_run_dir_detects_markers(tmp_path: Path) -> None:
    job = tmp_path / "claude"
    job.mkdir()
    (job / "run.json").write_text("{}", encoding="utf-8")
    assert not is_job_run_dir(job)

    (job / "trials").mkdir()
    assert is_job_run_dir(job)

    other = tmp_path / "other"
    other.mkdir()
    (other / "benchmark_job.json").write_text("{}", encoding="utf-8")
    assert is_job_run_dir(other)

    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "RELEASE.lock.json").write_text("{}", encoding="utf-8")
    assert is_job_run_dir(locked)


def test_iter_session_dirs_skips_job_root_before_trials_exist(tmp_path: Path) -> None:
    """Empty trials/ must not make the job folder look like a session."""
    job = tmp_path / "claude"
    (job / "trials").mkdir(parents=True)
    (job / "run.json").write_text(
        json.dumps({"job_id": "abc", "agent_type": "cli.claude"}),
        encoding="utf-8",
    )
    (job / "benchmark_job.json").write_text("{}", encoding="utf-8")
    assert iter_session_dirs(tmp_path) == []

    trial = job / "trials" / "case__t01"
    trial.mkdir()
    (trial / "run.json").write_text(
        json.dumps({"session_id": "case__t01", "scenario_name": "dc_clos"}),
        encoding="utf-8",
    )
    assert iter_session_dirs(tmp_path) == [trial]


def test_order_run_json_puts_agent_before_metadata() -> None:
    ordered = order_run_json(
        {
            "metadata": {"machine_identities": {}},
            "session_id": "s1",
            "session_dir": "/tmp/s1",
            "agent_type": "cli.claude",
            "model": "gpt-4o",
            "problem_names": ["link_down"],
            "status": "finished",
            "llm_provider": "openai",
            "scenario_name": "simple_bgp",
            "extra_flag": True,
        }
    )
    keys = list(ordered)
    assert keys.index("agent_type") < keys.index("metadata")
    assert keys.index("model") < keys.index("metadata")
    assert keys.index("problem_names") < keys.index("metadata")
    assert keys.index("session_dir") < keys.index("metadata")
    assert keys[-1] == "metadata"
    assert keys.index("extra_flag") < keys.index("metadata")
    assert keys[:8] == [
        "session_id",
        "status",
        "scenario_name",
        "problem_names",
        "agent_type",
        "llm_provider",
        "model",
        "session_dir",
    ]


def test_update_run_json_is_atomic_read_modify_write(tmp_path: Path) -> None:
    from nika.utils.session_artifacts import update_run_json

    assert update_run_json(tmp_path, lambda meta: None) is False
    (tmp_path / "run.json").write_text('{"status": "running"}', encoding="utf-8")
    assert update_run_json(tmp_path, lambda meta: meta.update(outcome="success"))
    assert json.loads((tmp_path / "run.json").read_text(encoding="utf-8")) == {
        "status": "running",
        "outcome": "success",
    }
    # No temp files left next to the document.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["run.json"]
