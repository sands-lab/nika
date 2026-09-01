"""Live Docker + LangGraph/DeepSeek E2E for the curated 0.2.0 test subset.

Requires Docker and ``DEEPSEEK_API_KEY``. Run:

    uv run pytest tests/benchmark/test_curated_release_e2e.py -m live -v
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from nika.workflows.benchmark.release import load_run_config
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import (
    expand_trials,
    is_valid_trial,
    scan_trials,
    trial_dir,
)
from nika.workflows.eval.summary import run_eval_summary
from nika.workflows.leaderboard.meta_input import write_submission_templates
from nika.workflows.leaderboard.pack import pack_leaderboard_submission
from nika.workflows.leaderboard.validate import validate_leaderboard_submission
from tests.benchmark.curated import CURATED_VERSION, freeze_curated_release
from tests.leaderboard.test_e2e_release_pack import _fill_staging
from tests.support.integration_pipeline import deepseek_api_key_available, load_test_env
from tests.support.prerequisites import docker_available

load_test_env()

_AGENT = "byo.langgraph"
_PROVIDER = "deepseek"
_MODEL = "deepseek-chat"
_MAX_STEPS = 15

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.live,
    pytest.mark.skipif(not docker_available(), reason="Docker required"),
    pytest.mark.skipif(
        not deepseek_api_key_available(),
        reason="DEEPSEEK_API_KEY required for curated live E2E",
    ),
]


class TestCuratedReleaseLiveE2E:
    """Real labs + LangGraph/DeepSeek on three curated 0.2.0 test rows."""

    def test_batch_resume_summary_pack_validate(self, tmp_path: Path) -> None:
        releases_root = tmp_path / "releases"
        release = freeze_curated_release(
            releases_root / CURATED_VERSION,
            n_trials=1,
        )
        result_dir = tmp_path / "curated-live-run"
        runs_dir = tmp_path / "benchmark_runs"

        with (
            patch("nika.workflows.benchmark.release.RELEASES_DIR", releases_root),
            patch(
                "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
                runs_dir,
            ),
        ):
            run_benchmark_from_release(
                release_ref=CURATED_VERSION,
                split="dev",
                agent_type=_AGENT,
                llm_provider=_PROVIDER,
                model=_MODEL,
                max_steps=_MAX_STEPS,
                batch_size=2,
                result_dir=str(result_dir),
                case_timeout=1200,
                continue_on_error=True,
                check_images=True,
                release=release,
            )

            job = load_run_config(result_dir)
            assert job is not None
            assert job["official"] is True
            assert job["agent_type"] == _AGENT
            assert job["model"] == _MODEL
            assert job["case_count"] == 3
            assert job["n_trials"] == 1

            expected = expand_trials(release.cases, release.n_trials)
            assert len(expected) == 3
            for trial in expected:
                path = trial_dir(result_dir, trial.case_key, trial.trial_index)
                assert is_valid_trial(path), path
                meta = json.loads((path / "run.json").read_text(encoding="utf-8"))
                assert meta["outcome"] in {"success", "agent_failed"}

            # Wipe one counted trial so resume must re-run only that trial.
            target = expected[0]
            wipe_path = trial_dir(result_dir, target.case_key, target.trial_index)
            shutil.rmtree(wipe_path)
            _, pending = scan_trials(
                trials=expected, result_dir=result_dir, resume=True
            )
            assert pending == [0]

            run_benchmark_from_release(
                release_ref=CURATED_VERSION,
                split="dev",
                agent_type=_AGENT,
                llm_provider=_PROVIDER,
                model=_MODEL,
                max_steps=_MAX_STEPS,
                batch_size=1,
                result_dir=str(result_dir),
                case_timeout=1200,
                continue_on_error=True,
                resume=True,
                check_images=False,
                release=release,
            )
            assert is_valid_trial(wipe_path)

            # Remaining trials must still be valid (were not deleted).
            for trial in expected[1:]:
                assert is_valid_trial(
                    trial_dir(result_dir, trial.case_key, trial.trial_index)
                )

            summary_csv = result_dir / "summary.csv"
            run_eval_summary(results_dir=str(result_dir), output_path=str(summary_csv))
            assert summary_csv.is_file()
            assert summary_csv.stat().st_size > 0

            staging = write_submission_templates(result_dir / "submission")
            _fill_staging(
                staging,
                name="Curated Live LangGraph",
                model=_MODEL,
                framework="langgraph",
                tags=["e2e", "curated", "live"],
            )
            package = pack_leaderboard_submission(
                result_dir, submission_dir=staging
            ).scores_dir
            report = validate_leaderboard_submission(package)
            assert report.ok, report.errors
            from nika.workflows.leaderboard.validate_trajectories import (
                validate_trajectory_package,
            )

            traj_report = validate_trajectory_package(
                package.parent / f"{package.name}_trajectories",
                scores_dir=package,
            )
            assert traj_report.ok, traj_report.errors
