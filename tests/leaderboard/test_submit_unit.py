"""Unit tests for leaderboard submit (mocked gh/git)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from nika.workflows.leaderboard.hf_remote import (
    remote_trajectories_relpath,
    trajectories_browser_url,
)
from nika.workflows.leaderboard.remote import (
    remote_submission_relpath,
    submission_branch_name,
)
from nika.workflows.leaderboard.submit import (
    LeaderboardSubmitError,
    _submit_packed_package,
    submit_leaderboard_package,
)
from nika.workflows.leaderboard.validate import LeaderboardValidateError


def _write_minimal_package(root: Path, *, version: str = "0.2.0") -> Path:
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
    results = root / "results"
    results.mkdir()
    (results / "identity.yaml").write_text(
        yaml.safe_dump(
            {
                "benchmark": {
                    "id": "nika-bench",
                    "version": version,
                    "ref": f"nika-bench@{version}",
                    "split": "test",
                    "case_count": 1,
                    "n_trials": 1,
                    "scoring_id": "rule-based",
                    "leaderboard_primary": "rca_f1",
                },
                "run": {
                    "run_id": "run-1",
                    "official": True,
                    "agent_type": "mock",
                    "model": "mock",
                    "case_timeout_sec": 2400,
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


def test_submit_pack_failure_stops_before_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.pack_leaderboard_submission",
        lambda *_a, **_k: (_ for _ in ()).throw(
            __import__(
                "nika.workflows.leaderboard.pack", fromlist=["LeaderboardPackError"]
            ).LeaderboardPackError("not an official run")
        ),
    )
    remote_called = {"gh": False}

    def boom(*_a, **_k):
        remote_called["gh"] = True
        raise AssertionError("should not reach remote")

    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.gh.ensure_gh_auth",
        boom,
    )
    with pytest.raises(LeaderboardSubmitError, match="pack failed"):
        submit_leaderboard_package(
            tmp_path / "results",
            submission_dir=tmp_path / "submission",
        )
    assert remote_called["gh"] is False


def test_remote_path_helpers() -> None:
    assert remote_submission_relpath("0.2.0", "20260101_unit") == (
        "submissions/0.2.0/20260101_unit"
    )
    assert submission_branch_name("20260101_unit") == "submission/20260101_unit"
    assert remote_trajectories_relpath("0.2.0", "20260101_unit") == (
        "trajectories/0.2.0/20260101_unit"
    )
    assert remote_trajectories_relpath(
        "0.2.0", "20260101_unit_trajectories"
    ) == ("trajectories/0.2.0/20260101_unit")
    assert trajectories_browser_url("0.2.0", "20260101_unit").endswith(
        "/trajectories/0.2.0/20260101_unit"
    )
    with pytest.raises(ValueError):
        remote_submission_relpath("../x", "y")


def test_submit_hf_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = _write_minimal_package(tmp_path / "20260101_unit_agent")
    traj = tmp_path / "20260101_unit_agent_trajectories"
    traj.mkdir()
    (traj / "metadata.yaml").write_text(
        (pkg / "metadata.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (traj / "README.md").write_text("# Unit\n", encoding="utf-8")
    (traj / "identity.yaml").write_text(
        yaml.safe_dump(
            {
                "created_at": "2026-01-01T00:00:00+00:00",
                "benchmark": {
                    "id": "nika-bench",
                    "version": "0.2.0",
                    "ref": "nika-bench@0.2.0",
                    "split": "test",
                    "case_count": 1,
                    "n_trials": 1,
                    "scoring_id": "rule-based",
                    "leaderboard_primary": "rca_f1",
                },
                "run": {
                    "run_id": "run-1",
                    "official": True,
                    "agent_type": "mock",
                    "model": "mock",
                    "case_timeout_sec": 2400,
                },
                "scores_package": "20260101_unit_agent",
                "trajectories_relpath": "trajectories/0.2.0/20260101_unit_agent",
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (traj / "trials").mkdir()

    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.hf.ensure_hf_token",
        lambda: "hf_test",
    )

    def fake_upload(**kwargs):
        from nika.workflows.leaderboard.hf_cli import HfPullRequestResult

        return HfPullRequestResult(
            repo_id=kwargs["repo_id"],
            remote_path=kwargs["path_in_repo"],
            pr_url="https://huggingface.co/datasets/Zhihao98/nika-trajectories/discussions/7",
            pr_num=7,
        )

    monkeypatch.setattr(
        "nika.workflows.leaderboard.submit.hf.upload_folder_create_pr",
        fake_upload,
    )

    result = _submit_packed_package(
        pkg,
        skip_validate=True,
        skip_github=True,
        trajectories_dir=traj,
    )
    assert result.pr_url is None
    assert result.trajectories_pr_num == 7
    assert result.trajectories_remote_path == (
        "trajectories/0.2.0/20260101_unit_agent"
    )


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
        _submit_packed_package(pkg, skip_validate=True, skip_trajectories=True)


def test_submit_validate_failure(tmp_path: Path) -> None:
    pkg = tmp_path / "bad_pkg"
    pkg.mkdir()
    (pkg / "README.md").write_text("x\n", encoding="utf-8")
    with pytest.raises(LeaderboardValidateError):
        _submit_packed_package(
            pkg, skip_validate=False, skip_trajectories=True
        )


def test_submit_direct_push_opens_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = _write_minimal_package(tmp_path / "20260101_unit_agent", version="0.2.0")

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

    result = _submit_packed_package(
        pkg,
        skip_validate=True,
        skip_trajectories=True,
        draft=True,
        work_dir=tmp_path / "work",
    )
    assert result.used_fork is False
    assert result.remote_path == "submissions/0.2.0/20260101_unit_agent"
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

    result = _submit_packed_package(
        pkg,
        skip_validate=True,
        skip_trajectories=True,
        work_dir=tmp_path / "work",
    )
    assert result.used_fork is True
    assert result.head == "tester:submission/20260101_unit_agent"
