"""Unit tests for Kubernetes workload image caching."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from nika.net_env.utils import k8s_workload_cache as cache
from tests.support.prerequisites import docker_available


@pytest.fixture(autouse=True)
def _isolate_cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "k8s-images")


def test_workload_images_for_supported_scenarios() -> None:
    assert cache.K8S_LAB_WORKLOAD_IMAGES
    assert cache.LLMD_LAB_WORKLOAD_IMAGES
    assert (
        cache.workload_images_for_scenario("k8s_lab") == cache.K8S_LAB_WORKLOAD_IMAGES
    )
    assert (
        cache.workload_images_for_scenario("llmd_lab") == cache.LLMD_LAB_WORKLOAD_IMAGES
    )
    assert cache.workload_images_for_scenario("dc_clos") == ()


def test_cache_tar_path_is_stable_and_unique() -> None:
    a = cache.cache_tar_path("quay.io/metallb/controller:v0.14.9")
    b = cache.cache_tar_path("postgres:16")
    assert a != b
    assert a.name.endswith(".tar")
    assert "quay.io" in a.name


def test_ensure_cached_skips_pull_and_save_when_tar_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tar_path = cache.cache_tar_path("postgres:16")
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tar_path.write_bytes(b"cached")

    with (
        patch.object(cache, "pull_image") as pull,
        patch.object(cache, "_get_client") as get_client,
    ):
        result = cache.ensure_cached("postgres:16")

    assert result == tar_path
    pull.assert_not_called()
    get_client.assert_not_called()


def test_ensure_cached_pulls_and_saves_when_missing(tmp_path: Path) -> None:
    saved = b"docker-tar"

    class _FakeImage:
        def save(self, *, named: bool = True):
            yield saved

    fake_client = MagicMock()
    fake_client.images.get.return_value = _FakeImage()

    with (
        patch.object(cache, "image_exists", return_value=False),
        patch.object(cache, "pull_image") as pull,
        patch.object(cache, "_get_client", return_value=fake_client),
    ):
        result = cache.ensure_cached("postgres:16")

    pull.assert_called_once_with("postgres:16")
    assert result == cache.cache_tar_path("postgres:16")
    assert result.read_bytes() == saved


def test_preload_signals_even_without_cached_tars() -> None:
    net_env = MagicMock()
    net_env.LAB_NAME = "llmd_lab"
    net_env.name = "llmd_lab__test"
    net_env.kubernetes_nodes = ["controller", "worker1"]
    runtime = MagicMock()
    net_env._build_runtime.return_value = runtime
    runtime.exec.return_value = "0"

    with patch.object(cache, "cached_tar_paths", return_value=[]):
        cache.preload_workload_images(net_env)

    runtime.exec.assert_any_call(
        "controller",
        f"mkdir -p /var/run && touch {cache._PRELOAD_SIGNAL_PATH}",
        timeout=15.0,
    )


def test_ensure_cached_returns_none_when_pull_fails() -> None:
    with (
        patch.object(cache, "image_exists", return_value=False),
        patch.object(cache, "pull_image", side_effect=RuntimeError("denied")),
    ):
        assert cache.ensure_cached("ghcr.io/example:v1") is None


def test_preload_signals_when_k3s_api_times_out() -> None:
    net_env = MagicMock()
    net_env.LAB_NAME = "llmd_lab"
    net_env.name = "llmd_lab__test"
    net_env.kubernetes_nodes = ["controller", "worker1"]
    runtime = MagicMock()
    net_env._build_runtime.return_value = runtime

    with patch.object(
        cache, "_wait_k3s_api", side_effect=TimeoutError("k3s API not ready")
    ):
        cache.preload_workload_images(net_env)

    runtime.exec.assert_any_call(
        "controller",
        f"mkdir -p /var/run && touch {cache._PRELOAD_SIGNAL_PATH}",
        timeout=15.0,
    )


def test_preload_imports_cached_tars_for_all_nodes() -> None:
    net_env = MagicMock()
    net_env.LAB_NAME = "llmd_lab"
    net_env.name = "llmd_lab__test"
    net_env.kubernetes_nodes = ["controller", "worker1"]
    runtime = MagicMock()
    net_env._build_runtime.return_value = runtime
    runtime.exec.return_value = "0"
    tar_a = Path("/tmp/a.tar")
    tar_b = Path("/tmp/b.tar")

    with (
        patch.object(cache, "cached_tar_paths", return_value=[tar_a, tar_b]),
        patch.object(cache, "import_tar_to_node") as import_tar,
    ):
        cache.preload_workload_images(net_env)

    assert import_tar.call_count == 4
    runtime.exec.assert_any_call(
        "controller",
        f"mkdir -p /var/run && touch {cache._PRELOAD_SIGNAL_PATH}",
        timeout=15.0,
    )


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Requires Docker")
def test_cache_scenario_writes_some_llmd_workload_tars() -> None:
    cache.cache_scenario("llmd_lab")
    assert cache.cached_tar_paths("llmd_lab")
