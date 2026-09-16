"""Single-case mock benchmark startup smoke."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from nika.workflows.benchmark.release import (
    JOB_FILENAME,
    RUN_CONFIG_FILENAME,
    freeze_release,
    load_run_config,
)
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import case_key_for_row, is_valid_trial, trial_dir
from tests.benchmark.trial_helpers import ROW_A, mini_cases_yaml
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]


def test_mini_benchmark_startup_smoke(tmp_path: Path) -> None:
    """One dc_clos/link_down case × 1 mock trial through release run."""
    source = mini_cases_yaml(tmp_path / "cases_src.yaml", rows=[ROW_A])
    release = freeze_release(
        version="ci-smoke-release",
        source_cases=source,
        out_dir=tmp_path / "releases" / "ci-smoke-release",
    )
    release = replace(release, defaults={**release.defaults, "n_trials": 1})
    assert release.case_count == 1
    assert release.n_trials == 1

    result_dir = tmp_path / "ci-smoke-run"
    runs_dir = tmp_path / "benchmark_runs"

    with patch(
        "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
        runs_dir,
    ):
        run_benchmark_from_release(
            release_ref="ci-smoke-release",
            split="dev",
            agent_type="mock",
            llm_provider=None,
            model="mock-v1",
            max_steps=20,
            result_dir=str(result_dir),
            case_timeout=0,
            check_images=False,
            release=release,
        )

    job = load_run_config(result_dir)
    assert job is not None
    assert (result_dir / RUN_CONFIG_FILENAME).is_file()
    assert (result_dir / JOB_FILENAME).is_file()

    key = case_key_for_row(release.cases[0])
    path = trial_dir(result_dir, key, 1)
    assert is_valid_trial(path)
    trial_meta = json.loads((path / "run.json").read_text(encoding="utf-8"))
    assert trial_meta["status"] == "finished"
    assert trial_meta["outcome"] in {"success", "agent_failed"}
