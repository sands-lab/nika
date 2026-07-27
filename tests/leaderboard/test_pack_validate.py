"""Tests for leaderboard pack / validate MVP."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from nika.workflows.benchmark.release import (
    RESOURCES_V1,
    SCORING_V1,
    TOOLS_V1,
    freeze_release,
    load_release_from_dir,
    write_release_manifest,
)
from nika.workflows.benchmark.trials import expand_trials, trial_dir
from nika.workflows.leaderboard.meta_input import (
    MetaInputError,
    load_metadata_file,
    slugify_name,
    write_submission_templates,
)
from nika.workflows.leaderboard.pack import (
    LeaderboardPackError,
    pack_leaderboard_submission,
)
from nika.workflows.leaderboard.schema import (
    FILES_FILENAME,
    IDENTITY_FILENAME,
    METADATA_FILENAME,
    METRICS_FILENAME,
    README_FILENAME,
    RESULTS_DIRNAME,
    SubmissionMetadata,
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


def _freeze_mini(
    tmp_path: Path,
    *,
    version: str = "lb-mini",
    n_trials: int = 1,
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


def _write_trial_artifacts(
    session_dir: Path,
    *,
    outcome: str = "success",
    rca_f1: float = 1.0,
) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        session_dir / "run.json",
        {
            "status": "finished",
            "outcome": outcome,
            "session_id": session_dir.name,
            "scenario_name": "simple_bgp",
            "root_cause_name": "link_down",
        },
    )
    _write_json(
        session_dir / "ground_truth.json",
        {
            "is_anomaly": True,
            "faulty_devices": ["pc1"],
            "root_cause_name": ["link_down"],
        },
    )
    (session_dir / "messages.jsonl").write_text(
        '{"role":"assistant","content":"ok"}\n', encoding="utf-8"
    )
    metrics = {
        "detection_score": 1.0 if outcome == "success" else -1.0,
        "localization_accuracy": 1.0 if outcome == "success" else -1.0,
        "localization_precision": 1.0 if outcome == "success" else -1.0,
        "localization_recall": 1.0 if outcome == "success" else -1.0,
        "localization_f1": 1.0 if outcome == "success" else -1.0,
        "rca_accuracy": rca_f1 if outcome == "success" else -1.0,
        "rca_precision": rca_f1 if outcome == "success" else -1.0,
        "rca_recall": rca_f1 if outcome == "success" else -1.0,
        "rca_f1": rca_f1 if outcome == "success" else -1.0,
        "in_tokens": 10,
        "out_tokens": 5,
        "steps": 2,
        "tool_calls": 2,
        "tool_errors": 0,
    }
    _write_json(session_dir / "eval_metrics.json", metrics)
    if outcome == "success":
        _write_json(
            session_dir / "submission.json",
            {
                "is_anomaly": True,
                "faulty_devices": ["pc1"],
                "root_cause_name": ["link_down"],
            },
        )


def _build_release_run(
    tmp_path: Path,
    release: Any,
    *,
    include_failed: bool = False,
) -> Path:
    result_dir = tmp_path / "results" / "run1"
    result_dir.mkdir(parents=True)
    run_cfg = {
        "run_id": "testrun01",
        "job_id": "testrun01",
        "benchmark_id": release.id,
        "version": release.version,
        "benchmark_ref": release.ref,
        "benchmark_digest": release.benchmark_digest,
        "split": release.split,
        "case_count": release.case_count,
        "cases_sha256": release.cases_sha256,
        "nika_git_commit": None,
        "nika_git_dirty": False,
        "scoring": release.scoring,
        "tools": release.tools,
        "resources": release.resources,
        "defaults": release.defaults,
        "case_timeout_sec": release.case_timeout_sec,
        "agent_type": "mock",
        "llm_provider": None,
        "model": "mock-v1",
        "max_steps": None,
        "n_trials": release.n_trials,
        "official": True,
    }
    _write_json(result_dir / "run.json", run_cfg)

    trials = expand_trials(release.cases, release.n_trials)
    for index, trial in enumerate(trials):
        session = trial_dir(result_dir, trial.case_key, trial.trial_index)
        if include_failed and index == len(trials) - 1:
            _write_trial_artifacts(session, outcome="agent_failed")
        else:
            _write_trial_artifacts(session, outcome="success", rca_f1=1.0)
    return result_dir


@pytest.fixture
def mini_release_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    release = _freeze_mini(tmp_path, n_trials=2)
    monkeypatch.setattr(
        "nika.workflows.benchmark.release.RELEASES_DIR",
        tmp_path / "releases",
    )
    result_dir = _build_release_run(tmp_path, release)
    return release, result_dir


_FULL_METADATA: dict[str, Any] = {
    "info": {
        "name": "Test Agent",
        "authors": "NIKA Test",
        "org": None,
        "site": None,
        "report": None,
        "logo": None,
        "email": None,
        "github": None,
    },
    "agent": {
        "model": "mock-v1",
        "framework": "mock",
        "tools": ["task_mcp_server"],
        "skills": ["network-diagnosis"],
        "optimization_methods": ["none"],
        "tags": ["test"],
        "os_model": False,
        "os_system": False,
        "extra": {},
    },
}

_DEFAULT_README = "# Test Agent\n\nTest README.\n"


def _pack(
    result_dir: Path,
    *,
    metadata: dict[str, Any] | None = None,
    readme_text: str | None = None,
    **overrides: Any,
) -> Path:
    meta = metadata if metadata is not None else _FULL_METADATA
    return pack_leaderboard_submission(
        result_dir,
        metadata=meta,
        readme_text=readme_text if readme_text is not None else _DEFAULT_README,
        **overrides,
    )


def _identity_path(package: Path) -> Path:
    return package / RESULTS_DIRNAME / IDENTITY_FILENAME


def _metrics_rel() -> str:
    return f"{RESULTS_DIRNAME}/{METRICS_FILENAME}"


def _identity_rel() -> str:
    return f"{RESULTS_DIRNAME}/{IDENTITY_FILENAME}"


def _refresh_package_hash(package: Path, rel: str) -> None:
    from nika.workflows.leaderboard.hashing import sha256_file

    files = json.loads((package / FILES_FILENAME).read_text(encoding="utf-8"))
    files["package"][rel] = sha256_file(package / rel)
    (package / FILES_FILENAME).write_text(
        json.dumps(files, indent=2) + "\n", encoding="utf-8"
    )


class TestLeaderboardPackValidate:
    def test_pack_rejects_missing_metadata(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        with pytest.raises(LeaderboardPackError, match="metadata is required"):
            pack_leaderboard_submission(result_dir)

    def test_pack_and_validate_full_metadata(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        meta = yaml.safe_load(yaml.safe_dump(_FULL_METADATA))
        meta["info"]["name"] = "Meta Agent"
        meta["info"]["org"] = "NIKA Lab"
        meta["info"]["site"] = "https://example.com/agent"
        meta["agent"]["framework"] = "langgraph"
        meta["agent"]["tools"] = ["kathara_base_mcp_server", "task_mcp_server"]
        meta["agent"]["optimization_methods"] = ["reflection"]
        meta["agent"]["tags"] = ["research"]
        package = _pack(result_dir, metadata=meta)
        assert package.name.endswith("_meta_agent")
        assert (package / METADATA_FILENAME).is_file()
        assert (package / README_FILENAME).is_file()
        assert (package / FILES_FILENAME).is_file()
        assert _identity_path(package).is_file()
        assert (package / RESULTS_DIRNAME / METRICS_FILENAME).is_file()
        packed = yaml.safe_load(
            (package / METADATA_FILENAME).read_text(encoding="utf-8")
        )
        assert packed["info"]["name"] == "Meta Agent"
        assert packed["agent"]["model"] == "mock-v1"
        assert packed["agent"]["framework"] == "langgraph"
        assert packed["agent"]["tools"] == [
            "kathara_base_mcp_server",
            "task_mcp_server",
        ]
        assert packed["agent"]["skills"] == ["network-diagnosis"]
        assert packed["agent"]["optimization_methods"] == ["reflection"]
        assert packed["agent"]["tags"] == ["research"]

        report = validate_leaderboard_submission(package, source_result_dir=result_dir)
        assert report.ok, report.errors

    def test_pack_from_submission_dir(self, mini_release_env, tmp_path: Path) -> None:
        _release, result_dir = mini_release_env
        staging = write_submission_templates(tmp_path / "submission")
        data = yaml.safe_load((staging / METADATA_FILENAME).read_text(encoding="utf-8"))
        data["info"]["name"] = "YAML Agent"
        data["info"]["authors"] = "Lab Authors"
        data["info"]["org"] = "Lab"
        data["info"]["site"] = "https://example.com/agent"
        data["agent"]["model"] = "gpt-4.1"
        data["agent"]["framework"] = "autogen"
        data["agent"]["tools"] = ["pingmesh_mcp_server"]
        data["agent"]["tags"] = ["yaml"]
        data["agent"]["extra"] = {"note": "from-dir"}
        (staging / METADATA_FILENAME).write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
        (staging / README_FILENAME).write_text(
            "# YAML Agent\n\nFrom staging dir.\n", encoding="utf-8"
        )
        package = pack_leaderboard_submission(result_dir, submission_dir=staging)
        packed = yaml.safe_load(
            (package / METADATA_FILENAME).read_text(encoding="utf-8")
        )
        assert packed["info"]["name"] == "YAML Agent"
        assert packed["agent"]["model"] == "gpt-4.1"
        assert packed["agent"]["framework"] == "autogen"
        assert packed["agent"]["extra"]["note"] == "from-dir"
        assert "From staging dir" in (package / README_FILENAME).read_text(
            encoding="utf-8"
        )
        assert validate_leaderboard_submission(package).ok

    def test_write_and_load_submission_templates(self, tmp_path: Path) -> None:
        staging = write_submission_templates(tmp_path / "submission")
        assert (staging / METADATA_FILENAME).is_file()
        assert (staging / README_FILENAME).is_file()
        with pytest.raises(MetaInputError):
            load_metadata_file(staging / METADATA_FILENAME)
        data = yaml.safe_load((staging / METADATA_FILENAME).read_text(encoding="utf-8"))
        data["info"]["name"] = "Filled"
        data["info"]["authors"] = "Author One"
        data["info"]["email"] = "agent@example.com"
        data["info"]["github"] = "example-lab"
        data["agent"]["model"] = "gpt-4.1"
        data["agent"]["framework"] = "langgraph"
        (staging / METADATA_FILENAME).write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
        metadata = load_metadata_file(staging / METADATA_FILENAME)
        assert isinstance(metadata, SubmissionMetadata)
        assert metadata.info.name == "Filled"
        assert metadata.info.email == "agent@example.com"
        assert metadata.info.github == "example-lab"
        assert metadata.agent.model == "gpt-4.1"
        assert metadata.agent.framework == "langgraph"
        assert slugify_name("SWE-agent + Claude 3.5") == "swe_agent_claude_3_5"

    def test_missing_agent_in_package_fails_validate(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        meta_path = package / METADATA_FILENAME
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        data["agent"] = None
        meta_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        _refresh_package_hash(package, METADATA_FILENAME)
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("invalid metadata" in e.lower() for e in report.errors)

    def test_digest_mismatch_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        identity_path = _identity_path(package)
        data = yaml.safe_load(identity_path.read_text(encoding="utf-8"))
        data["benchmark"]["digest"] = "0" * 64
        identity_path.write_text(
            yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
        )
        _refresh_package_hash(package, _identity_rel())
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("digest" in e for e in report.errors)

    def test_missing_trial_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        trials_root = package / RESULTS_DIRNAME / "trials"
        trial_dirs = sorted(trials_root.iterdir())
        shutil.rmtree(trial_dirs[0])
        files = json.loads((package / FILES_FILENAME).read_text(encoding="utf-8"))
        removed = [
            k
            for k in list(files["package"])
            if k.startswith(f"{RESULTS_DIRNAME}/trials/{trial_dirs[0].name}/")
        ]
        for key in removed:
            files["package"].pop(key, None)
        (package / FILES_FILENAME).write_text(
            json.dumps(files, indent=2) + "\n", encoding="utf-8"
        )
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("missing trials" in e for e in report.errors)

    def test_duplicate_trial_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        trials_root = package / RESULTS_DIRNAME / "trials"
        trials = sorted(trials_root.iterdir())
        src = trials[0]
        second = trials[1]
        shutil.rmtree(second)
        shutil.copytree(src, second)
        from nika.workflows.leaderboard.hashing import sha256_file

        files = json.loads((package / FILES_FILENAME).read_text(encoding="utf-8"))
        rel = f"{RESULTS_DIRNAME}/trials/{second.name}/result.json"
        files["package"][rel] = sha256_file(second / "result.json")
        (package / FILES_FILENAME).write_text(
            json.dumps(files, indent=2) + "\n", encoding="utf-8"
        )
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any(
            "duplicate trial_id" in e
            or "trial directory name" in e
            or "missing trials" in e
            for e in report.errors
        )

    def test_metrics_inconsistency_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        metrics_path = package / RESULTS_DIRNAME / METRICS_FILENAME
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics["mean_rca_f1"] = 0.0
        metrics_path.write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
        _refresh_package_hash(package, _metrics_rel())
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("mean_rca_f1" in e for e in report.errors)

    def test_tampered_package_file_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        metrics_path = package / RESULTS_DIRNAME / METRICS_FILENAME
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        metrics_path.write_text(json.dumps(metrics, indent=4) + "\n", encoding="utf-8")
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("package file modified" in e for e in report.errors)

    def test_bad_agent_type_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        meta_path = package / METADATA_FILENAME
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        data["agent"]["tools"] = "not-a-list"
        meta_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        _refresh_package_hash(package, METADATA_FILENAME)
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any(
            "invalid metadata" in e.lower() or "agent" in e for e in report.errors
        )

    def test_secret_in_metadata_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        meta_path = package / METADATA_FILENAME
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        data["info"]["org"] = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
        meta_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        _refresh_package_hash(package, METADATA_FILENAME)
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("secret" in e for e in report.errors)

    def test_absolute_path_in_metadata_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        meta_path = package / METADATA_FILENAME
        data = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        data["info"]["org"] = "/home/wang/secret-lab"
        meta_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        _refresh_package_hash(package, METADATA_FILENAME)
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("absolute path" in e for e in report.errors)

    def test_missing_readme_fails_validate(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        (package / README_FILENAME).unlink()
        report = validate_leaderboard_submission(package)
        assert not report.ok
        assert any("README.md" in e for e in report.errors)

    def test_source_run_tamper_fails(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        package = _pack(result_dir)
        run_path = result_dir / "run.json"
        data = json.loads(run_path.read_text(encoding="utf-8"))
        data["tampered"] = True
        run_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        report = validate_leaderboard_submission(package, source_result_dir=result_dir)
        assert not report.ok
        assert any("source run.json modified" in e for e in report.errors)

    def test_pack_rejects_non_official_run(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        run_path = result_dir / "run.json"
        data = json.loads(run_path.read_text(encoding="utf-8"))
        data["official"] = False
        run_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        with pytest.raises(LeaderboardPackError, match="official"):
            _pack(result_dir)

    def test_pack_rejects_incomplete_trials(self, mini_release_env) -> None:
        _release, result_dir = mini_release_env
        trial = next((result_dir / "trials").iterdir())
        (trial / "eval_metrics.json").unlink()
        with pytest.raises(LeaderboardPackError, match="Incomplete|missing"):
            _pack(result_dir)
