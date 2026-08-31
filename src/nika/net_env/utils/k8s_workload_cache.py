"""Host-side cache and k3s sideload for Kubernetes lab workload images."""

from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from nika.config import REPO_ROOT
from nika.net_env.utils.kathara.docker_files.docker_images import (
    _get_client,
    ensure_nika_docker_images,
    image_exists,
    pull_image,
)

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

K8S_LAB = "k8s_lab"
LLMD_LAB = "llmd_lab"

K8S_LAB_HOST_IMAGES = (
    "nika/frr",
    "rancher/k3s:v1.34.1-k3s1",
    "nika/base",
)

LLMD_LAB_HOST_IMAGES = (
    "rancher/k3s:v1.34.1-k3s1",
    "nika/base",
)

K8S_LAB_WORKLOAD_IMAGES = (
    "quay.io/metallb/controller:v0.14.9",
    "quay.io/metallb/speaker:v0.14.9",
    "quay.io/frrouting/frr:9.1.0",
    "registry.k8s.io/ingress-nginx/controller:v1.12.0",
    "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.5.0",
    "postgres:16",
    "ik2227/word",
    "ik2227/weather",
)

LLMD_LAB_WORKLOAD_IMAGES = (
    "quay.io/metallb/controller:v0.16.1",
    "quay.io/metallb/speaker:v0.16.1",
    "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
    "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
    "ghcr.io/llm-d/llm-d-inference-sim:latest",
)

K8S_SCENARIOS = frozenset({K8S_LAB, LLMD_LAB})

_PRELOAD_SIGNAL_PATH = "/var/run/nika-images-preloaded"
_MOUNT_CACHE_DIR = "/nika-image-cache"
_K3S_API_WAIT_SEC = 600.0
_K3S_API_POLL_SEC = 2.0
_IMPORT_TIMEOUT_SEC = 600.0


def cache_root() -> Path:
    return REPO_ROOT / ".nika_cache" / "k8s-images"


def workload_images_for_scenario(scenario: str) -> tuple[str, ...]:
    if scenario == K8S_LAB:
        return K8S_LAB_WORKLOAD_IMAGES
    if scenario == LLMD_LAB:
        return LLMD_LAB_WORKLOAD_IMAGES
    return ()


def host_images_for_scenario(scenario: str) -> tuple[str, ...]:
    if scenario == K8S_LAB:
        return K8S_LAB_HOST_IMAGES
    if scenario == LLMD_LAB:
        return LLMD_LAB_HOST_IMAGES
    return ()


def cache_tar_path(image: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "__", image)
    return cache_root() / f"{safe}.tar"


def cache_tar_exists(image: str) -> bool:
    path = cache_tar_path(image)
    return path.is_file() and path.stat().st_size > 0


def ensure_cached(image: str) -> Path | None:
    """Pull ``image`` on the host when needed and return its tar cache path."""
    tar_path = cache_tar_path(image)
    if cache_tar_exists(image):
        return tar_path

    cache_root().mkdir(parents=True, exist_ok=True)
    try:
        if not image_exists(image):
            pull_image(image)
    except RuntimeError as exc:
        print(f"WARNING: skipping cache for {image}: {exc}")
        return None

    print(f"Caching Docker image {image} -> {tar_path.name}...")
    client = _get_client()
    try:
        with tar_path.open("wb") as handle:
            for chunk in client.images.get(image).save(named=True):
                handle.write(chunk)
    except Exception as exc:  # noqa: BLE001 - continue caching other images
        print(f"WARNING: could not save {image} to cache: {exc}")
        tar_path.unlink(missing_ok=True)
        return None
    return tar_path


def ensure_workload_cache(scenario: str) -> list[Path]:
    """Ensure host tar caches exist for a scenario's workload images."""
    cached: list[Path] = []
    for image in workload_images_for_scenario(scenario):
        tar_path = ensure_cached(image)
        if tar_path is not None:
            cached.append(tar_path)
    return cached


def cache_scenario(scenario: str) -> None:
    """Pre-pull host lab images and workload image tars for ``scenario``."""
    host_images = host_images_for_scenario(scenario)
    if host_images:
        ensure_nika_docker_images(host_images)
    workload = workload_images_for_scenario(scenario)
    if workload:
        ensure_workload_cache(scenario)
    if scenario == LLMD_LAB:
        from nika.net_env.llmd_lab.lab import ensure_helm_charts

        try:
            ensure_helm_charts()
        except Exception as exc:  # noqa: BLE001 - charts fall back to OCI at deploy
            print(f"WARNING: could not cache llmd_lab Helm charts: {exc}")


def cached_tar_paths(scenario: str) -> list[Path]:
    return [
        cache_tar_path(image)
        for image in workload_images_for_scenario(scenario)
        if cache_tar_exists(image)
    ]


def _wait_k3s_api(runtime: LabRuntime, controller: str = "controller") -> None:
    deadline = time.time() + _K3S_API_WAIT_SEC
    while time.time() < deadline:
        output = runtime.exec(
            controller,
            "kubectl api-versions >/dev/null 2>&1; echo $?",
            timeout=30.0,
        ).strip()
        if output.endswith("0"):
            return
        time.sleep(_K3S_API_POLL_SEC)
    raise TimeoutError(
        f"k3s API not ready on {controller!r} within {_K3S_API_WAIT_SEC}s"
    )


def mount_workload_cache(machine, scenario: str) -> None:
    """Mount cached image archives read-only when this scenario has a cache."""
    if cached_tar_paths(scenario):
        machine.add_meta("volume", f"{cache_root()}|{_MOUNT_CACHE_DIR}|ro")


def import_tar_to_node(runtime: LabRuntime, node: str, tar_path: Path) -> None:
    remote_path = f"{_MOUNT_CACHE_DIR}/{tar_path.name}"
    runtime.exec(
        node,
        f"k3s ctr -n k8s.io images import {remote_path}",
        timeout=_IMPORT_TIMEOUT_SEC,
    )


def signal_preload_complete(
    runtime: LabRuntime, controller: str = "controller"
) -> None:
    runtime.exec(
        controller,
        f"mkdir -p /var/run && touch {_PRELOAD_SIGNAL_PATH}",
        timeout=15.0,
    )


def _import_all_tars_to_node(
    runtime: LabRuntime, node: str, tar_paths: list[Path]
) -> None:
    for tar_path in tar_paths:
        import_tar_to_node(runtime, node, tar_path)


def preload_workload_images(net_env: NetworkEnvBase) -> None:
    """Import cached workload images into k3s nodes before bootstrap applies manifests."""
    scenario = getattr(net_env, "LAB_NAME", None) or net_env.name
    if scenario not in K8S_SCENARIOS:
        return

    runtime = net_env._build_runtime()
    controller = "controller"
    try:
        try:
            _wait_k3s_api(runtime, controller)
        except TimeoutError:
            print(
                f"WARNING: k3s API not ready; skipping workload image preload for {scenario}"
            )
            return

        tar_paths = cached_tar_paths(scenario)
        nodes = list(getattr(net_env, "kubernetes_nodes", []) or [])
        if not nodes:
            nodes = [name for name in runtime.list_nodes() if name.startswith("worker")]
            nodes.insert(0, controller)

        if tar_paths:
            print(
                f"Preloading {len(tar_paths)} cached workload image(s) "
                f"into {len(nodes)} k3s node(s) for {scenario}..."
            )
            with ThreadPoolExecutor(max_workers=min(6, len(nodes))) as pool:
                futures = {
                    pool.submit(
                        _import_all_tars_to_node, runtime, node, tar_paths
                    ): node
                    for node in nodes
                }
                for future in as_completed(futures):
                    future.result()
    finally:
        signal_preload_complete(runtime, controller)
