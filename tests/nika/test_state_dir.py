"""State root resolution and Docker bind-mount preflight."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika import config
from nika.net_env.utils import k8s_workload_cache as cache

pytestmark = pytest.mark.unit

_GIT_IDENT = {
    "GIT_AUTHOR_NAME": "nika",
    "GIT_AUTHOR_EMAIL": "nika@test",
    "GIT_COMMITTER_NAME": "nika",
    "GIT_COMMITTER_EMAIL": "nika@test",
}


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, **_GIT_IDENT},
    )


def test_git_common_dir_parent_is_state_dir() -> None:
    raw = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=config.REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    common = Path(raw)
    if not common.is_absolute():
        common = config.REPO_ROOT / common
    assert config.resolve_state_dir() == common.resolve().parent


def test_worktree_shares_main_checkout(tmp_path, monkeypatch) -> None:
    main = tmp_path / "main"
    worktree = tmp_path / "worktree"
    main.mkdir()
    _git(main, "init")
    _git(main, "commit", "--allow-empty", "-m", "init")
    _git(main, "worktree", "add", str(worktree), "HEAD")
    monkeypatch.setattr(config, "REPO_ROOT", worktree)
    assert config.resolve_state_dir() == main.resolve()


def test_fallback_when_not_a_git_checkout(monkeypatch) -> None:
    def _no_git(*_args, **_kwargs):
        raise OSError("no git")

    monkeypatch.setattr(config.subprocess, "run", _no_git)
    assert config.resolve_state_dir() == config.REPO_ROOT


def test_nfs_bind_path_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cache.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="nfs\n")
    )
    with pytest.raises(RuntimeError, match="NFS"):
        cache.ensure_docker_bind_path(tmp_path / "missing" / "x")


def test_sandbox_package_caches_follow_state_dir():
    from nika.agents.sandbox import npm, wheels

    assert npm._NPM_CACHE == config.CACHE_DIR / "agent-npm"
    assert wheels._WHEEL_CACHE == config.CACHE_DIR / "agent-wheels"


def test_local_bind_path_accepted(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cache.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="ext2/ext3\n")
    )
    cache.ensure_docker_bind_path(tmp_path)
