"""Single-case benchmark smoke through the installed user CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent.protocols import DIAGNOSIS, SUBMISSION
from nika.utils.session_store import SessionStore
from nika.workflows.benchmark.trials import (
    case_key_for_row,
    is_valid_trial,
    trial_dir,
    trial_dirname,
)
from nika.workflows.session.close import close_session
from tests.benchmark.trial_helpers import ROW_A, mini_cases_yaml
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_mini_benchmark_startup_smoke(tmp_path: Path) -> None:
    """Run the documented YAML benchmark path and require a successful trial."""
    cases_path = mini_cases_yaml(tmp_path / "cases.yaml", rows=[ROW_A])
    result_dir = tmp_path / "ci-smoke-run"
    subprocess_tmp = tmp_path / "tmp"
    subprocess_tmp.mkdir()
    key = case_key_for_row(ROW_A)
    expected_session_id = trial_dirname(key, 1)
    store = SessionStore()
    running_before = {str(row["session_id"]) for row in store.list_running_sessions()}

    try:
        proc = subprocess.run(
            [
                "uv",
                "run",
                "nika",
                "benchmark",
                "run",
                "--config",
                str(cases_path),
                "--agent",
                "mock",
                "--model",
                "mock-v1",
                "--max-steps",
                "20",
                "--batch-size",
                "1",
                "--case-timeout",
                "300",
                "--abort-on-error",
                "--result_dir",
                str(result_dir),
            ],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=False,
            env={**os.environ, "TMPDIR": str(subprocess_tmp)},
            text=True,
            timeout=360,
        )
        output = proc.stdout + proc.stderr
        assert proc.returncode == 0, output
        assert "benchmark_done" in output
    finally:
        for row in store.list_running_sessions():
            session_id = str(row["session_id"])
            session_dir = Path(str(row.get("session_dir") or ""))
            if session_id not in running_before and session_dir.is_relative_to(
                result_dir
            ):
                close_session(session_id=session_id)

    path = trial_dir(result_dir, key, 1)
    assert is_valid_trial(path)
    trial_meta = json.loads((path / "run.json").read_text(encoding="utf-8"))
    assert trial_meta["status"] == "finished"
    assert trial_meta["outcome"] == "success"
    assert trial_meta["session_id"] == expected_session_id

    for name in (
        "ground_truth.json",
        "messages.jsonl",
        "submission.json",
        "eval_metrics.json",
    ):
        assert (path / name).is_file(), f"missing benchmark artifact: {name}"

    messages = [
        json.loads(line)
        for line in (path / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert {entry["phase"] for entry in messages} >= {DIAGNOSIS, SUBMISSION}

    metrics = json.loads((path / "eval_metrics.json").read_text(encoding="utf-8"))
    assert metrics["detection_score"] == 1.0
    assert metrics["rca_accuracy"] == 1.0
    assert metrics["tool_calls"] > 0

    with pytest.raises(FileNotFoundError):
        store.get_session(expected_session_id)
