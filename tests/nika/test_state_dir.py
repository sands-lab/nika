"""State root resolution and Docker bind-mount preflight."""

from types import SimpleNamespace

import pytest

from nika import config
from nika.net_env.utils import k8s_workload_cache as cache

pytestmark = pytest.mark.unit


@pytest.fixture
def run_cfg(tmp_path, monkeypatch):
    def write(state_dir):
        path = tmp_path / "nika.yaml"
        path.write_text(
            f"nika:\n  state_dir: {state_dir}\n" if state_dir else "nika: {}\n"
        )
        monkeypatch.setenv("NIKA_RUN_CONFIG", str(path))

    return write


def test_default_is_repo_root(run_cfg, monkeypatch):
    monkeypatch.delenv("NIKA_HOME", raising=False)
    run_cfg(None)
    assert config.resolve_state_dir() == config.REPO_ROOT


def test_config_key_then_env_override(run_cfg, monkeypatch, tmp_path):
    monkeypatch.delenv("NIKA_HOME", raising=False)
    run_cfg(str(tmp_path / "cfg"))
    assert config.resolve_state_dir() == (tmp_path / "cfg").resolve()
    monkeypatch.setenv("NIKA_HOME", str(tmp_path / "env"))
    assert config.resolve_state_dir() == (tmp_path / "env").resolve()


def test_relative_resolves_from_repo(monkeypatch):
    monkeypatch.setenv("NIKA_HOME", "state")
    assert config.resolve_state_dir() == (config.REPO_ROOT / "state").resolve()


def test_nfs_bind_path_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(
        cache.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="nfs\n")
    )
    with pytest.raises(RuntimeError, match="NIKA_HOME"):
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
