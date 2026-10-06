"""Unit tests for Kubernetes workload image caching."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from nika.config import REPO_ROOT
from nika.net_env.utils import k8s_workload_cache as cache


@pytest.fixture(autouse=True)
def _isolate_cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "cache_root", lambda: tmp_path / "k8s-images")


def _image_directory(directory: Path, *, complete: bool = True) -> str:
    """Minimal OCI graph with real content hashes, suitable for dir transport."""
    directory.mkdir(parents=True, exist_ok=True)
    descriptors = []
    for content in (
        json.dumps({"os": "linux", "architecture": cache.host_machine_arch()}).encode(),
        b"layer",
    ):
        digest = hashlib.sha256(content).hexdigest()
        if complete or content != b"layer":
            (directory / digest).write_bytes(content)
        descriptors.append(
            {
                "digest": "sha256:" + digest,
                "size": len(content),
                "mediaType": "application/octet-stream",
            }
        )
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": descriptors[0],
            "layers": [descriptors[1]],
        }
    ).encode()
    (directory / "manifest.json").write_bytes(manifest)
    return "postgres:16@sha256:" + hashlib.sha256(manifest).hexdigest()


def _image_tar(tmp_path: Path, *, complete: bool = True) -> tuple[str, bytes]:
    directory = tmp_path / "source"
    image = _image_directory(directory, complete=complete)
    path = tmp_path / "test.tar"
    cache._write_oci_archive(directory, path, image)
    return image, path.read_bytes()


@pytest.mark.unit
def test_workload_images_for_supported_scenarios() -> None:
    from nika.net_env.k8s_lab.lab import K8sFatTreeBGP
    from nika.net_env.llmd_lab.lab import LLMDInferenceCluster

    k8s_images = K8sFatTreeBGP.K8S_WORKLOAD_IMAGES
    llmd_images = LLMDInferenceCluster.K8S_WORKLOAD_IMAGES
    assert k8s_images
    assert llmd_images
    assert cache.K3S_SYSTEM_IMAGES
    assert cache.workload_images_for_scenario("k8s_lab") == k8s_images
    assert cache.workload_images_for_scenario("llmd_lab") == llmd_images
    assert cache.workload_images_for_scenario("dc_clos") == ()
    assert all(
        "@sha256:" in image for image in (*k8s_images, *llmd_images, cache.K3S_IMAGE)
    )
    for image in cache.K3S_SYSTEM_IMAGES:
        assert image in k8s_images
        assert image in llmd_images


@pytest.mark.unit
def test_workload_cache_covers_scenario_manifests() -> None:
    def image_refs(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "image" and isinstance(item, str):
                    assert value.get("imagePullPolicy") == "Never", item
                    yield item
                else:
                    yield from image_refs(item)
        elif isinstance(value, list):
            for item in value:
                yield from image_refs(item)

    for scenario in ("k8s_lab", "llmd_lab"):
        root = REPO_ROOT / "src" / "nika" / "net_env" / scenario
        manifest_images = {
            image
            for path in root.rglob("*.yaml")
            for document in yaml.safe_load_all(path.read_text())
            for image in image_refs(document)
        }
        assert manifest_images <= set(cache.workload_images_for_scenario(scenario))


@pytest.mark.unit
def test_ensure_cached_skips_network_when_valid_archive_exists(tmp_path: Path) -> None:
    image, saved = _image_tar(tmp_path)
    tar_path = cache.cache_tar_path(image)
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tar_path.write_bytes(saved)
    with patch.object(cache, "_skopeo") as fetch:
        assert cache.ensure_cached(image) == tar_path
    fetch.assert_not_called()


@pytest.mark.unit
@pytest.mark.parametrize("replacement_valid", [False, True])
def test_cache_upgrade_preserves_legacy_until_replacement_validates(
    tmp_path: Path, replacement_valid: bool
) -> None:
    image, saved = _image_tar(tmp_path)
    target = cache.cache_tar_path(image)
    target.parent.mkdir(parents=True, exist_ok=True)
    legacy = target.parent / "postgres__16.tar"
    legacy.write_bytes(b"legacy archive")
    unrelated = target.parent / "unrelated.tar"
    unrelated.write_bytes(b"unrelated archive")
    if replacement_valid:
        target.write_bytes(saved)
    with (
        patch.object(cache, "workload_images_for_scenario", return_value=(image,)),
        patch.object(cache, "ensure_nika_docker_images"),
        patch.object(cache, "_skopeo", side_effect=RuntimeError("denied")),
    ):
        if replacement_valid:
            assert cache.ensure_workload_cache("k8s_lab") == [target]
        else:
            with pytest.raises(RuntimeError, match="denied"):
                cache.ensure_workload_cache("k8s_lab")
    assert legacy.exists() is not replacement_valid
    assert unrelated.read_bytes() == b"unrelated archive"


@pytest.mark.unit
def test_ensure_cached_fetches_complete_graph_when_missing(tmp_path: Path) -> None:
    image, _ = _image_tar(tmp_path)

    def fetch(*args, mount, timeout):
        if args[0] == "inspect":
            return (tmp_path / "source" / "manifest.json").read_bytes()
        assert "--preserve-digests" in args
        _image_directory(Path(args[-1].removeprefix("dir:")))

    with (
        patch.object(cache, "_skopeo", side_effect=fetch),
        patch.object(cache, "ensure_nika_docker_images"),
    ):
        path = cache.ensure_cached(image)
    assert cache._tar_is_complete(path, image)
    with tarfile.open(path) as archive:
        index = json.load(archive.extractfile("index.json"))
        assert {
            root["annotations"]["io.containerd.image.name"]
            for root in index["manifests"]
        } == {
            "docker.io/library/postgres:16",
            image.replace("postgres:16", "docker.io/library/postgres"),
        }


@pytest.mark.unit
def test_ensure_cached_rejects_archives_missing_layers(tmp_path: Path) -> None:
    image, saved = _image_tar(tmp_path, complete=False)
    tar_path = cache.cache_tar_path(image)
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tar_path.write_bytes(saved)
    assert not cache._tar_is_complete(tar_path, image)

    def fetch(*args, mount, timeout):
        if args[0] == "inspect":
            return (tmp_path / "source" / "manifest.json").read_bytes()
        _image_directory(Path(args[-1].removeprefix("dir:")), complete=False)

    with (
        patch.object(cache, "_skopeo", side_effect=fetch),
        patch.object(cache, "ensure_nika_docker_images"),
        pytest.raises(RuntimeError, match="Incomplete or corrupt"),
    ):
        cache.ensure_cached(image)


@pytest.mark.unit
def test_archive_rejects_corrupt_content_and_wrong_pin(tmp_path: Path) -> None:
    image, saved = _image_tar(tmp_path)
    path = tmp_path / "corrupt.tar"
    path.write_bytes(saved.replace(b"layer", b"wrong"))
    assert not cache._tar_is_complete(path, image)
    path.write_bytes(saved)
    assert not cache._tar_is_complete(
        path, image.replace(image.split("@")[1], "sha256:" + "0" * 64)
    )


@pytest.mark.unit
def test_platform_archive_preserves_upstream_index_identity(tmp_path: Path) -> None:
    directory = tmp_path / "platform"
    image = _image_directory(directory)
    selected = directory / "manifest.json"
    manifest = selected.read_bytes()
    selected.rename(directory / (image.split("sha256:")[1] + ".manifest.json"))
    index = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": image.split("@")[1],
                    "size": len(manifest),
                    "platform": {
                        "os": "linux",
                        "architecture": cache.host_machine_arch(),
                    },
                },
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:" + "0" * 64,
                    "size": 10,
                    "platform": {"os": "windows", "architecture": "amd64"},
                },
            ],
        }
    ).encode()
    selected.write_bytes(index)
    pinned = "postgres:16@sha256:" + hashlib.sha256(index).hexdigest()
    archive = tmp_path / "platform.tar"
    cache._write_oci_archive(directory, archive, pinned)
    assert cache._tar_is_complete(archive, pinned)
    with tarfile.open(archive) as tar:
        roots = json.load(tar.extractfile("index.json"))["manifests"]
        assert all(root["digest"] == pinned.split("@")[1] for root in roots)


@pytest.mark.unit
def test_preload_raises_without_cached_tars() -> None:
    net_env = MagicMock()
    net_env.LAB_NAME = "llmd_lab"
    net_env.name = "llmd_lab__test"
    net_env.kubernetes_nodes = ["controller", "worker1"]
    runtime = MagicMock()
    net_env._build_runtime.return_value = runtime

    with (
        patch.object(cache, "cache_scenario"),
        patch.object(cache, "cached_tar_paths", return_value=[]),
        pytest.raises(RuntimeError, match="No workload images available"),
    ):
        cache.preload_workload_images(net_env)

    assert not any(
        "nika-images-preloaded" in str(call) for call in runtime.exec.call_args_list
    )


@pytest.mark.unit
def test_ensure_cached_reports_registry_failure(tmp_path: Path) -> None:
    image, _ = _image_tar(tmp_path)
    with (
        patch.object(cache, "ensure_nika_docker_images"),
        patch.object(cache, "_skopeo", side_effect=RuntimeError("denied")),
        pytest.raises(RuntimeError, match="denied"),
    ):
        cache.ensure_cached(image)
    assert not cache.cache_tar_exists(image)


@pytest.mark.unit
def test_preload_records_stopped_node_before_failure_cleanup() -> None:
    net_env = MagicMock()
    net_env.LAB_NAME = "k8s_lab"
    net_env.name = "k8s_lab__test"
    net_env.kubernetes_nodes = ["controller", "worker3"]
    runtime = net_env._build_runtime.return_value
    container = runtime.get_container.return_value
    container.id = "worker3-container-id"
    container.attrs = {
        "State": {"Status": "exited", "ExitCode": 137, "OOMKilled": True}
    }
    container.logs.return_value = b"k3s agent exited\n"
    runtime.exec.return_value = ""
    container.status = "running"
    tars = [
        cache.cache_tar_path(image)
        for image in cache.workload_images_for_scenario("k8s_lab")
    ]

    def import_image(_runtime, node, _tar):
        if node == "worker3":
            container.status = "exited"
            raise RuntimeError("container is not running")

    with (
        patch.object(cache, "cache_scenario"),
        patch.object(cache, "cached_tar_paths", return_value=tars),
        patch.object(cache, "_wait_k3s_api"),
        patch.object(cache, "import_tar_to_node", side_effect=import_image),
        patch("nika.utils.logger.log_error_event") as log_error,
        pytest.raises(RuntimeError, match="worker3 while importing "),
    ):
        cache.preload_workload_images(net_env)

    event = log_error.call_args
    assert event.args[0] == "env_preload_node_failed"
    assert event.kwargs["node"] == "worker3"
    assert event.kwargs["image_tar"] == tars[0].name
    assert event.kwargs["container_id"] == "worker3-container-id"
    assert event.kwargs["container_state"]["ExitCode"] == 137
    assert event.kwargs["container_state"]["OOMKilled"] is True
    assert "k3s agent exited" in event.kwargs["container_logs_tail"]
    assert not any(
        "nika-images-preloaded" in str(call) for call in runtime.exec.call_args_list
    )


@pytest.mark.unit
def test_import_tar_to_node_requires_exit_marker() -> None:
    runtime = MagicMock()
    runtime.exec.return_value = "some docker noise without marker"
    with pytest.raises(RuntimeError, match="Failed to import"):
        cache.import_tar_to_node(runtime, "controller", Path("/tmp/x.tar"))


@pytest.mark.unit
def test_import_tar_to_node_accepts_success_marker() -> None:
    runtime = MagicMock()
    runtime.exec.return_value = "imported\nNIKA_IMPORT_EXIT:0\n"
    cache.import_tar_to_node(runtime, "controller", Path("/tmp/x.tar"))
