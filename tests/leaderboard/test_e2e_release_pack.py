"""End-to-end: mocked release run → template → pack → validate (no Docker)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from nika.cli.main import app
from nika.workflows.benchmark.release import (
    RESOURCES_V1,
    SCORING_V1,
    TOOLS_V1,
    freeze_release,
    load_release_from_dir,
    load_run_config,
    write_release_manifest,
)
from nika.workflows.benchmark.run import run_benchmark_from_release
from nika.workflows.benchmark.trials import expand_trials, is_valid_trial, trial_dir
from nika.workflows.leaderboard.meta_input import (
    slugify_name,
    write_submission_templates,
)
from nika.workflows.leaderboard.pack import pack_leaderboard_submission
from nika.workflows.leaderboard.schema import (
    FILES_FILENAME,
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    README_FILENAME,
    RESULTS_DIRNAME,
)
from nika.workflows.leaderboard.validate import validate_leaderboard_submission


def _mini_cases_yaml(path: Path) -> Path:
    payload = {
        "seed": 42,
        "cases": [
            {
                "scenario": "simple_bgp",
                "topo_size": None,
                "problem": "link_down",
                "inject": {"host_name": "pc1", "intf_name": "eth0"},
            }
        ],
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _freeze_mini_release(
    tmp_path: Path,
    *,
    version: str = "lb-e2e",
    n_trials: int = 2,
) -> Any:
    source = _mini_cases_yaml(tmp_path / "cases_src.yaml")
    dest = tmp_path / "releases" / version
    release = freeze_release(version=version, source_cases=source, out_dir=dest)
    defaults = dict(release.defaults)
    defaults["n_trials"] = n_trials
    write_release_manifest(
        dest,
        version=version,
        splits=release.splits,
        defaults=defaults,
        scoring=dict(SCORING_V1),
        tools=dict(TOOLS_V1),
        resources=dict(RESOURCES_V1),
        images=release.images,
        scenario_problem_pin=release.scenario_problem_pin,
    )
    return load_release_from_dir(dest, split="dev", verify_digest=True)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_mocked_trial(
    session_dir: Path,
    *,
    trial_id: str,
    trial_index: int,
    case_key: str,
    scenario: str,
    problem: str,
    release_meta: dict[str, Any] | None,
    outcome: str = "success",
    rca_f1: float = 1.0,
) -> None:
    """Write a complete trial tree as if a mock agent finished the pipeline."""
    session_dir.mkdir(parents=True, exist_ok=True)
    run_meta: dict[str, Any] = {
        "session_id": trial_id,
        "status": "finished",
        "outcome": outcome,
        "trial_id": trial_id,
        "trial_index": trial_index,
        "case_key": case_key,
        "scenario_name": scenario,
        "root_cause_name": problem,
        "agent_type": (release_meta or {}).get("agent_type", "mock"),
        "model": (release_meta or {}).get("model", "mock-v1"),
    }
    if release_meta:
        run_meta["benchmark_run_id"] = release_meta.get("run_id")
        run_meta["benchmark_id"] = release_meta.get("benchmark_id")
        run_meta["benchmark_version"] = release_meta.get("version")
        run_meta["benchmark_digest"] = release_meta.get("benchmark_digest")
        run_meta["benchmark_split"] = release_meta.get("split")
    _write_json(session_dir / "run.json", run_meta)
    _write_json(
        session_dir / "ground_truth.json",
        {
            "is_anomaly": True,
            "faulty_devices": ["pc1"],
            "root_cause_name": [problem],
        },
    )
    (session_dir / "messages.jsonl").write_text(
        json.dumps({"role": "assistant", "content": "mock diagnosis"}) + "\n",
        encoding="utf-8",
    )
    success = outcome == "success"
    _write_json(
        session_dir / "eval_metrics.json",
        {
            "detection_score": 1.0 if success else -1.0,
            "localization_accuracy": 1.0 if success else -1.0,
            "localization_precision": 1.0 if success else -1.0,
            "localization_recall": 1.0 if success else -1.0,
            "localization_f1": 1.0 if success else -1.0,
            "rca_accuracy": rca_f1 if success else -1.0,
            "rca_precision": rca_f1 if success else -1.0,
            "rca_recall": rca_f1 if success else -1.0,
            "rca_f1": rca_f1 if success else -1.0,
            "in_tokens": 20,
            "out_tokens": 8,
            "steps": 3,
            "tool_calls": 3,
            "tool_errors": 0 if success else 1,
        },
    )
    if success:
        _write_json(
            session_dir / "submission.json",
            {
                "is_anomaly": True,
                "faulty_devices": ["pc1"],
                "root_cause_name": [problem],
            },
        )


def _fill_staging(staging: Path, *, name: str, **agent_overrides: Any) -> None:
    data = yaml.safe_load((staging / METADATA_FILENAME).read_text(encoding="utf-8"))
    data["info"]["name"] = name
    data["info"]["authors"] = "NIKA CI"
    data["info"]["org"] = "NIKA CI"
    data["info"]["site"] = "https://example.com/nika-e2e"
    data["agent"]["model"] = agent_overrides.get("model", "mock-v1")
    data["agent"]["framework"] = agent_overrides.get("framework", "mock")
    data["agent"]["tools"] = agent_overrides.get(
        "tools", ["task_mcp_server", "kathara_base_mcp_server"]
    )
    data["agent"]["skills"] = agent_overrides.get("skills", ["network-diagnosis"])
    data["agent"]["optimization_methods"] = agent_overrides.get(
        "optimization_methods", ["none"]
    )
    data["agent"]["tags"] = agent_overrides.get("tags", ["e2e", "mock"])
    data["agent"]["extra"] = agent_overrides.get(
        "extra", {"pipeline": "release-run-to-pack"}
    )
    (staging / METADATA_FILENAME).write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    (staging / README_FILENAME).write_text(
        f"# {name}\n\nE2E mock submission.\n", encoding="utf-8"
    )


class TestReleaseRunToLeaderboardPackE2E:
    """No Docker: mock per-trial execution, then pack/validate a submission."""

    def test_release_run_template_pack_validate(self, tmp_path: Path) -> None:
        release = _freeze_mini_release(tmp_path, n_trials=2)
        result_dir = tmp_path / "results" / "e2e-run"
        runs_dir = tmp_path / "benchmark_runs"

        def fake_trial(trial, **kwargs):
            path = trial_dir(
                Path(kwargs["result_dir"]), trial.case_key, trial.trial_index
            )
            _write_mocked_trial(
                path,
                trial_id=trial.trial_id,
                trial_index=trial.trial_index,
                case_key=trial.case_key,
                scenario=str(trial.row["scenario"]),
                problem=str(trial.row["problem"]),
                release_meta=kwargs.get("release_meta"),
                outcome="success" if trial.trial_index == 1 else "agent_failed",
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

            job = load_run_config(result_dir)
            assert job is not None
            assert job["official"] is True
            assert job["agent_type"] == "mock"
            assert job["model"] == "mock-v1"
            assert job["n_trials"] == 2
            assert job["case_count"] == 1
            assert job["benchmark_digest"] == release.benchmark_digest
            assert (result_dir / "RELEASE.lock.json").is_file()

            expected = expand_trials(release.cases, release.n_trials)
            assert len(expected) == 2
            for trial in expected:
                path = trial_dir(result_dir, trial.case_key, trial.trial_index)
                assert is_valid_trial(path), path

            staging = write_submission_templates(result_dir / "submission")
            _fill_staging(staging, name="E2E Mock Agent")

            package = pack_leaderboard_submission(
                result_dir,
                submission_dir=staging,
            )

        slug = slugify_name("E2E Mock Agent")
        assert package.name.endswith(f"_{slug}")
        assert package.parent == result_dir.resolve()
        assert (package / METADATA_FILENAME).is_file()
        assert (package / README_FILENAME).is_file()
        assert (package / FILES_FILENAME).is_file()
        assert (package / RESULTS_DIRNAME / IDENTITY_FILENAME).is_file()
        assert (package / RESULTS_DIRNAME / METRICS_FILENAME).is_file()

        identity = yaml.safe_load(
            (package / RESULTS_DIRNAME / IDENTITY_FILENAME).read_text(encoding="utf-8")
        )
        assert identity["schema_version"] == "1"
        assert identity["benchmark"]["version"] == release.version
        assert identity["benchmark"]["digest"] == release.benchmark_digest
        assert identity["benchmark"]["n_trials"] == 2
        assert identity["run"]["official"] is True
        assert identity["run"]["agent_type"] == "mock"

        packed_meta = yaml.safe_load(
            (package / METADATA_FILENAME).read_text(encoding="utf-8")
        )
        assert packed_meta["info"]["name"] == "E2E Mock Agent"
        assert packed_meta["agent"]["model"] == "mock-v1"
        assert packed_meta["agent"]["framework"] == "mock"
        assert packed_meta["agent"]["tags"] == ["e2e", "mock"]

        metrics = json.loads(
            (package / RESULTS_DIRNAME / METRICS_FILENAME).read_text(encoding="utf-8")
        )
        assert metrics["n_trials_expected"] == 2
        assert metrics["n_trials_present"] == 2
        assert metrics["n_success"] == 1
        assert metrics["n_agent_failed"] == 1
        assert metrics["mean_rca_f1"] == pytest.approx(0.5)
        assert metrics["primary_metric"] == "rca_f1"

        trial_dirs = sorted((package / RESULTS_DIRNAME / "trials").iterdir())
        assert len(trial_dirs) == 2

        with patch(
            "nika.workflows.benchmark.release.RELEASES_DIR",
            tmp_path / "releases",
        ):
            report = validate_leaderboard_submission(
                package, source_result_dir=result_dir
            )
        assert report.ok, report.errors

    def test_cli_template_then_pack_submission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI path: template → edit → pack --submission → validate."""
        release = _freeze_mini_release(tmp_path, version="lb-cli-e2e", n_trials=1)
        result_dir = tmp_path / "results" / "cli-e2e"
        runs_dir = tmp_path / "benchmark_runs"
        monkeypatch.setattr(
            "nika.workflows.benchmark.release.RELEASES_DIR",
            tmp_path / "releases",
        )

        def fake_trial(trial, **kwargs):
            path = trial_dir(
                Path(kwargs["result_dir"]), trial.case_key, trial.trial_index
            )
            _write_mocked_trial(
                path,
                trial_id=trial.trial_id,
                trial_index=trial.trial_index,
                case_key=trial.case_key,
                scenario=str(trial.row["scenario"]),
                problem=str(trial.row["problem"]),
                release_meta=kwargs.get("release_meta"),
            )

        with (
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
                release_ref=release.version,
                split="dev",
                agent_type="mock",
                llm_provider=None,
                model="mock-v1",
                max_steps=None,
                result_dir=str(result_dir),
                case_timeout=0,
                check_images=False,
                release=release,
            )

        runner = CliRunner()
        staging = result_dir / "submission"
        template_result = runner.invoke(
            app,
            ["leaderboard", "template", "-o", str(staging)],
        )
        assert template_result.exit_code == 0, template_result.output
        assert (staging / METADATA_FILENAME).is_file()
        assert (staging / README_FILENAME).is_file()

        _fill_staging(
            staging,
            name="CLI E2E Agent",
            tools=["task_mcp_server"],
            skills=[],
            optimization_methods=[],
            tags=["cli"],
            extra={},
        )

        pack_result = runner.invoke(
            app,
            [
                "leaderboard",
                "pack",
                "--result_dir",
                str(result_dir),
                "--submission",
                str(staging),
            ],
        )
        assert pack_result.exit_code == 0, pack_result.output
        slug = slugify_name("CLI E2E Agent")
        packages = [
            p
            for p in result_dir.iterdir()
            if p.is_dir() and p.name.endswith(f"_{slug}")
        ]
        assert len(packages) == 1
        package = packages[0]

        validate_result = runner.invoke(
            app,
            [
                "leaderboard",
                "validate",
                str(package),
                "--source-result-dir",
                str(result_dir),
            ],
        )
        assert validate_result.exit_code == 0, validate_result.output
        assert "validation passed" in validate_result.output.lower()
