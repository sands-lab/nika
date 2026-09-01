"""Contract tests for the curated 0.2.0 test-split fixture (no Docker)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from nika.workflows.benchmark.release import ReleaseError, load_release
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import expand_trials, is_valid_trial, trial_dir
from nika.workflows.leaderboard.meta_input import (
    slugify_name,
    write_submission_templates,
)
from nika.workflows.leaderboard.pack import pack_leaderboard_submission
from nika.workflows.leaderboard.schema import (
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    README_FILENAME,
    RESULTS_DIRNAME,
)
from nika.workflows.leaderboard.validate import validate_leaderboard_submission
from tests.benchmark.curated import (
    CURATED_VERSION,
    RELEASE_TEST_YAML,
    case_identity,
    freeze_curated_release,
    load_curated_cases,
)
from tests.leaderboard.test_e2e_release_pack import _fill_staging, _write_mocked_trial


@pytest.mark.contract
def test_curated_rows_are_subset_of_0_2_0_test_split() -> None:
    curated = load_curated_cases()
    assert len(curated) == 3
    release_cases = yaml.safe_load(RELEASE_TEST_YAML.read_text(encoding="utf-8"))[
        "cases"
    ]
    release_ids = {case_identity(row) for row in release_cases}
    for row in curated:
        assert case_identity(row) in release_ids, (
            f"curated row not in 0.2.0 test.yaml: "
            f"{row.get('scenario')} / {row.get('problem')} / {row.get('topo_size')}"
        )


@pytest.mark.contract
def test_deprecated_0_1_0_message_points_to_0_2_0() -> None:
    with pytest.raises(ReleaseError, match=r"0\.2\.0"):
        load_release("0.1.0", split="test")


@pytest.mark.contract
def test_curated_freeze_pack_validate(tmp_path: Path) -> None:
    """In-test freeze of curated rows packs and validates as an official mini run."""
    releases_root = tmp_path / "releases"
    release = freeze_curated_release(
        releases_root / CURATED_VERSION,
        n_trials=1,
    )
    assert release.case_count == 3
    assert release.n_trials == 1
    result_dir = tmp_path / "results"
    runs_dir = tmp_path / "benchmark_runs"

    def fake_trial(trial, **kwargs):
        path = trial_dir(Path(kwargs["result_dir"]), trial.case_key, trial.trial_index)
        _write_mocked_trial(
            path,
            trial_id=trial.trial_id,
            trial_index=trial.trial_index,
            case_key=trial.case_key,
            scenario=str(trial.row["scenario"]),
            problem=str(trial.row["problem"]),
            release_meta=kwargs.get("release_meta"),
            outcome="success",
            rca_f1=1.0,
        )

    with (
        patch("nika.workflows.benchmark.release.RELEASES_DIR", releases_root),
        patch("nika.workflows.benchmark.run.preflight_release"),
        patch(
            "nika.workflows.benchmark.run._run_trial_with_timeout",
            side_effect=fake_trial,
        ),
        patch(
            "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
            runs_dir,
        ),
    ):
        run_benchmark_from_release(
            release_ref=CURATED_VERSION,
            split="dev",
            agent_type="mock",
            llm_provider=None,
            model="mock-v1",
            max_steps=10,
            batch_size=2,
            result_dir=str(result_dir),
            case_timeout=0,
            check_images=False,
            release=release,
        )

        expected = expand_trials(release.cases, release.n_trials)
        assert len(expected) == 3
        for trial in expected:
            assert is_valid_trial(trial_dir(result_dir, trial.case_key, trial.trial_index))

        staging = write_submission_templates(result_dir / "submission")
        _fill_staging(staging, name="Curated Mock Agent")
        package = pack_leaderboard_submission(result_dir, submission_dir=staging).scores_dir

    assert package.name.endswith(f"_{slugify_name('Curated Mock Agent')}")
    assert (package / METADATA_FILENAME).is_file()
    assert (package / README_FILENAME).is_file()
    assert (package / RESULTS_DIRNAME / IDENTITY_FILENAME).is_file()
    assert (package / RESULTS_DIRNAME / METRICS_FILENAME).is_file()

    with patch("nika.workflows.benchmark.release.RELEASES_DIR", releases_root):
        report = validate_leaderboard_submission(package)
    assert report.ok, report.errors

    traj = package.parent / f"{package.name}_trajectories"
    assert traj.is_dir()
    from nika.workflows.leaderboard.validate_trajectories import (
        validate_trajectory_package,
    )

    with patch("nika.workflows.benchmark.release.RELEASES_DIR", releases_root):
        traj_report = validate_trajectory_package(traj, scores_dir=package)
    assert traj_report.ok, traj_report.errors


@pytest.mark.contract
def test_curated_timeout_continues_remaining(tmp_path: Path) -> None:
    """Timeout on one curated trial finalizes agent_failed; others still run."""
    releases_root = tmp_path / "releases"
    release = freeze_curated_release(releases_root / CURATED_VERSION, n_trials=1)
    result_dir = tmp_path / "results"
    runs_dir = tmp_path / "benchmark_runs"
    timed_out: set[str] = set()

    def flaky_trial(trial, **kwargs):
        from nika.workflows.benchmark.run import _finalize_timed_out_trial

        if not timed_out:
            timed_out.add(trial.trial_id)
            err = RuntimeError(
                f"[{trial.trial_id}] case exceeded --case-timeout (1s) and was killed."
            )
            # Seed a partial session so finalize can count the trial.
            path = trial_dir(Path(kwargs["result_dir"]), trial.case_key, trial.trial_index)
            path.mkdir(parents=True, exist_ok=True)
            (path / "run.json").write_text(
                '{"session_id":"%s","status":"running"}' % trial.trial_id,
                encoding="utf-8",
            )
            (path / "ground_truth.json").write_text("{}", encoding="utf-8")
            with (
                patch("nika.workflows.benchmark.run.close_session"),
                patch(
                    "nika.workflows.benchmark.run.run_eval_metrics",
                    side_effect=RuntimeError("no closed session"),
                ),
                patch(
                    "nika.workflows.benchmark.run.Session.load_closed_session",
                    side_effect=FileNotFoundError("gone"),
                ),
            ):
                _finalize_timed_out_trial(
                    trial, result_dir=kwargs["result_dir"], error=err
                )
            raise err
        path = trial_dir(Path(kwargs["result_dir"]), trial.case_key, trial.trial_index)
        _write_mocked_trial(
            path,
            trial_id=trial.trial_id,
            trial_index=trial.trial_index,
            case_key=trial.case_key,
            scenario=str(trial.row["scenario"]),
            problem=str(trial.row["problem"]),
            release_meta=kwargs.get("release_meta"),
            outcome="success",
        )

    with (
        patch("nika.workflows.benchmark.release.RELEASES_DIR", releases_root),
        patch("nika.workflows.benchmark.run.preflight_release"),
        patch(
            "nika.workflows.benchmark.run._run_trial_with_timeout",
            side_effect=flaky_trial,
        ),
        patch(
            "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
            runs_dir,
        ),
    ):
        run_benchmark_from_release(
            release_ref=CURATED_VERSION,
            split="dev",
            agent_type="mock",
            llm_provider=None,
            model="mock-v1",
            max_steps=10,
            batch_size=1,
            result_dir=str(result_dir),
            case_timeout=1,
            continue_on_error=True,
            check_images=False,
            release=release,
        )

    expected = expand_trials(release.cases, release.n_trials)
    assert len(timed_out) == 1
    for trial in expected:
        path = trial_dir(result_dir, trial.case_key, trial.trial_index)
        assert is_valid_trial(path), path
