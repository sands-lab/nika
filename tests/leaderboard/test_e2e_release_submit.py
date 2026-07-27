"""E2E: mocked release run → pack → validate → mocked submit (no network)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from nika.workflows.benchmark.release import load_run_config
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import trial_dir
from nika.workflows.leaderboard.meta_input import (
    slugify_name,
    write_submission_templates,
)
from nika.workflows.leaderboard.pack import pack_leaderboard_submission
from nika.workflows.leaderboard.submit import submit_leaderboard_package
from nika.workflows.leaderboard.validate import validate_leaderboard_submission

from tests.leaderboard.test_e2e_release_pack import (
    _fill_staging,
    _freeze_mini_release,
    _write_mocked_trial,
)


def test_release_pack_validate_submit_mocked(tmp_path: Path, monkeypatch) -> None:
    release = _freeze_mini_release(tmp_path, n_trials=2)
    result_dir = tmp_path / "results" / "e2e-submit"
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
        patch(
            "nika.workflows.benchmark.release.RELEASES_DIR",
            tmp_path / "releases",
        ),
        patch("nika.workflows.benchmark.run.preflight_release"),
        patch(
            "nika.workflows.benchmark.run._run_trial_with_timeout",
            side_effect=fake_trial,
        ),
        patch(
            "nika.workflows.benchmark.run_progress.BENCHMARK_RUNS_DIR",
            runs_dir,
        ),
        patch(
            "nika.workflows.leaderboard.validate.load_release",
            return_value=release,
        ),
    ):
        run_benchmark_from_release(
            release_ref=release.version,
            split="dev",
            agent_type="mock",
            llm_provider=None,
            model="mock-v1",
            max_steps=10,
            result_dir=str(result_dir),
            case_timeout=0,
            check_images=False,
            release=release,
        )
        assert load_run_config(result_dir) is not None

        staging = write_submission_templates(result_dir / "submission")
        _fill_staging(staging, name="Submit Mock Agent")
        package = pack_leaderboard_submission(
            result_dir,
            submission_dir=staging,
        )
        report = validate_leaderboard_submission(package, source_result_dir=result_dir)
        assert report.ok, report.errors

        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.ensure_gh_auth",
            lambda: None,
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.can_push",
            lambda _r: True,
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.clone_url_for_repo",
            lambda r: f"mock://{r}",
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.current_login",
            lambda: "ci",
        )

        def fake_clone(url, dest, *, depth=1):
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".git").mkdir()

        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.git_clone",
            fake_clone,
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.git_checkout_new_branch",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.git_add_all",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.git_commit",
            lambda *_a, **_k: True,
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.git_push",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr(
            "nika.workflows.leaderboard.submit.gh.create_pull_request",
            lambda **kwargs: "https://github.com/sands-lab/nika-leaderboard/pull/99",
        )

        # Mini freeze is not the shipped in-tree release; validate already ran above.
        result = submit_leaderboard_package(
            package,
            skip_validate=True,
            work_dir=tmp_path / "submit-work",
        )

    slug = slugify_name("Submit Mock Agent")
    assert package.name.endswith(f"_{slug}")
    assert result.pr_url.endswith("/pull/99")
    assert result.remote_path.endswith(f"/{package.name}")
    assert result.branch == f"submission/{package.name}"
