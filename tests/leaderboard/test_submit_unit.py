"""Unit tests for leaderboard submit (mocked gh/git)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from nika.workflows.leaderboard.remote import (
    remote_submission_relpath,
    submission_branch_name,
)
from nika.workflows.leaderboard.submit import (
    LeaderboardSubmitError,
    submit_leaderboard_package,
)
from nika.workflows.leaderboard.validate import LeaderboardValidateError


def _write_minimal_package(root: Path, *, version: str = "0.1.0") -> Path:
    """Write a package that submit can path-resolve (skip_validate=True)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "metadata.yaml").write_text(
        yaml.safe_dump(
            {
                "info": {"name": "Unit Agent", "authors": "CI"},
                "agent": {
                    "model": "mock",
                    "framework": "mock",
                    "tools": [],
                    "skills": [],
                    "optimization_methods": [],
                    "tags": [],
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (root / "README.md").write_text("# Unit Agent\n", encoding="utf-8")
    (root / "files.json").write_text(
        json.dumps({"source_run_sha256": "abc", "package": {}}) + "\n",
        encoding="utf-8",
    )
    results = root / "results"
    results.mkdir()
    (results / "identity.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1",
                "benchmark": {
                    "id": "nika-bench",
                    "version": version,
                    "digest": "d" * 64,
                    "split": "test",
                    "cases_sha256": "c" * 64,
                    "case_count": 1,
                    "n_trials": 1,
                    "scoring_id": "rule-based-v1",
                    "leaderboard_primary": "rca_f1",
                },
                "run": {
                    "run_id": "run-1",
                    "official": True,
                    "agent_type": "mock",
                    "model": "mock",
                    "n_trials": 1,
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (results / "metrics.json").write_text("{}\n", encoding="utf-8")
    trial = results / "trials" / "case__t01"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text("{}\n", encoding="utf-8")
    return root


def test_remote_path_helpers() -> None:
    assert remote_submission_relpath("0.1.0", "20260101_unit") == (
        "submissions/0.1.0/20260101_unit"
    )
    assert submission_branch_name("20260101_unit") == "submission/20260101_unit"
    with pytest.raises(ValueError):
        remote_submission_relpath("../x", "y")


def test_submit_requires_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_minimal_package(tmp_path / "20260101_unit_agent")
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.ensure_gh_auth",
        lambda: (_ for _ in ()).throw(
            __import__(
                "nika.workflows.leaderboard.github_cli", fromlist=["GitHubCliError"]
            ).GitHubCliError("not authenticated")
        ),
    )
    with pytest.raises(LeaderboardSubmitError, match="not authenticated"):
        submit_leaderboard_package(pkg, skip_validate=True)


def test_submit_validate_failure(tmp_path: Path) -> None:
    pkg = tmp_path / "bad_pkg"
    pkg.mkdir()
    (pkg / "README.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(LeaderboardValidateError):
        submit_leaderboard_package(pkg, skip_validate=False)


def test_submit_direct_push_opens_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _write_minimal_package(tmp_path / "20260101_unit_agent", version="0.1.0")

    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.ensure_gh_auth",
        lambda: None,
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.can_push",
        lambda _repo: True,
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.clone_url_for_repo",
        lambda repo: f"mock://{repo}",
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.current_login",
        lambda: "tester",
    )

    cloned: dict[str, Any] = {}

    def fake_clone(url: str, dest: Path, *, depth: int | None = 1) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".git").mkdir()
        (dest / "submissions").mkdir()
        cloned["dest"] = dest

    branches: list[str] = []
    pushes: list[str] = []
    prs: list[dict[str, Any]] = []

    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.git_clone",
        fake_clone,
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.git_checkout_new_branch",
        lambda _d, branch: branches.append(branch),
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.git_add_all",
        lambda _d: None,
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.git_commit",
        lambda _d, _msg: True,
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.git_push",
        lambda _d, *, branch, remote="origin": pushes.append(branch),
    )

    def fake_pr(**kwargs: Any) -> str:
        prs.append(kwargs)
        return "https://github.com/sands-lab/nika-leaderboard/pull/1"

    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.create_pull_request",
        fake_pr,
    )

    result = submit_leaderboard_package(
        pkg,
        skip_validate=True,
        draft=True,
        work_dir=tmp_path / "work",
    )
    assert result.used_fork is False
    assert result.remote_path == "submissions/0.1.0/20260101_unit_agent"
    assert result.branch == "submission/20260101_unit_agent"
    assert result.head == "submission/20260101_unit_agent"
    assert result.pr_url.endswith("/pull/1")
    assert branches == ["submission/20260101_unit_agent"]
    assert pushes == ["submission/20260101_unit_agent"]
    assert prs[0]["draft"] is True
    assert prs[0]["head"] == "submission/20260101_unit_agent"
    dest = cloned["dest"] / result.remote_path
    assert (dest / "metadata.yaml").is_file()


def test_submit_uses_fork_when_no_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _write_minimal_package(tmp_path / "20260101_unit_agent")

    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.ensure_gh_auth",
        lambda: None,
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.can_push",
        lambda _repo: False,
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.ensure_fork",
        lambda _repo: "tester/nika-leaderboard",
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.clone_url_for_repo",
        lambda repo: f"mock://{repo}",
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.current_login",
        lambda: "tester",
    )
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.git_clone",
        lambda url, dest, *, depth=1: (
            dest.mkdir(parents=True) or (dest / ".git").mkdir()
        ),
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
        lambda **kwargs: "https://github.com/sands-lab/nika-leaderboard/pull/2",
    )

    result = submit_leaderboard_package(
        pkg, skip_validate=True, work_dir=tmp_path / "work"
    )
    assert result.used_fork is True
    assert result.head == "tester:submission/20260101_unit_agent"
