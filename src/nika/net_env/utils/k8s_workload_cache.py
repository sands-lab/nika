"""Host-side cache and k3s sideload for Kubernetes lab workload images."""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from nika.config import REPO_ROOT
from nika.net_env.utils.kathara.docker_files.docker_images import (
    _get_client,
    ensure_nika_docker_images,
    image_exists,
    pull_image,
)
from nika.utils.parallel import bounded_parallel_map

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

# Bundled with rancher/k3s:v1.34.1-k3s1; nodes have no registry egress in lab.
K3S_SYSTEM_IMAGES = (
    "rancher/mirrored-pause:3.6",
    "rancher/mirrored-coredns-coredns:1.12.3",
    "rancher/mirrored-metrics-server:v0.8.0",
    "rancher/local-path-provisioner:v0.0.32",
)

K8S_LAB_WORKLOAD_IMAGES = (
    *K3S_SYSTEM_IMAGES,
    "quay.io/metallb/controller:v0.14.9",
    "quay.io/metallb/speaker:v0.14.9",
    "quay.io/frrouting/frr:9.1.0",
    "registry.k8s.io/ingress-nginx/controller:v1.12.0",
    "registry.k8s.io/ingress-nginx/kube-webhook-certgen:v1.5.0",
    "postgres:16",
    "ik2227/word:latest",
    "ik2227/weather:latest",
)

LLMD_LAB_WORKLOAD_IMAGES = (
    *K3S_SYSTEM_IMAGES,
    "quay.io/metallb/controller:v0.16.1",
    "quay.io/metallb/speaker:v0.16.1",
    "ghcr.io/llm-d/llm-d-router-endpoint-picker:v0.9.0",
    "ghcr.io/llm-d/llm-d-router-disagg-sidecar:v0.9.0",
    "ghcr.io/llm-d/llm-d-inference-sim:latest",
    # agentgateway Helm chart v1.1.0 (llmd_lab/lab.py _AGENTGATEWAY_VERSION):
    # the controller defaults to the chart appVersion and deploys proxies
    # with the same release tag.
    "cr.agentgateway.dev/controller:v1.1.0",
    "cr.agentgateway.dev/agentgateway:v1.1.0",
)

K8S_SCENARIOS = frozenset({K8S_LAB, LLMD_LAB})

_PRELOAD_SIGNAL_PATH = "/var/run/nika-images-preloaded"
_MOUNT_CACHE_DIR = "/nika-image-cache"
_K3S_API_WAIT_SEC = 600.0
_K3S_API_POLL_SEC = 2.0
_IMPORT_TIMEOUT_SEC = 600.0
# Serial imports avoid Kathara exec races and containerd sock contention.
_IMPORT_NODE_WORKERS = 1


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


def _progress(message: str) -> None:
    """Emit preload progress to stderr (visible under Rich Live) and nika.jsonl."""
    print(f"[k8s-cache] {message}", file=sys.stderr, flush=True)
    try:
        from nika.utils.logger import log_event

        log_event("env_preload_progress", message)
    except Exception:  # noqa: BLE001 - logging must not break preload
        pass


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
        print(f"WARNING: skipping cache for {image}: {exc}", file=sys.stderr)
        return None

    client = _get_client()
    try:
        with tar_path.open("wb") as handle:
            for chunk in client.images.get(image).save(named=True):
                handle.write(chunk)
    except Exception as exc:  # noqa: BLE001 - continue caching other images
        print(f"WARNING: could not save {image} to cache: {exc}", file=sys.stderr)
        tar_path.unlink(missing_ok=True)
        return None
    return tar_path


def ensure_workload_cache(scenario: str) -> list[Path]:
    """Ensure host tar caches exist for a scenario's workload images."""
    images = workload_images_for_scenario(scenario)
    already = sum(1 for image in images if cache_tar_exists(image))
    cached: list[Path] = []
    for image in images:
        tar_path = ensure_cached(image)
        if tar_path is not None:
            cached.append(tar_path)
    built = len(cached) - already
    if images:
        _progress(
            f"image cache for {scenario}: {len(cached)}/{len(images)} tar(s) "
            f"({already} hit, {max(built, 0)} built)"
        )
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
            print(
                f"WARNING: could not cache llmd_lab Helm charts: {exc}", file=sys.stderr
            )


def cached_tar_paths(scenario: str) -> list[Path]:
    return [
        cache_tar_path(image)
        for image in workload_images_for_scenario(scenario)
        if cache_tar_exists(image)
    ]


def _wait_k3s_api(runtime: LabRuntime, controller: str = "controller") -> None:
    deadline = time.time() + _K3S_API_WAIT_SEC
    started = time.time()
    last_log = 0.0
    while time.time() < deadline:
        output = runtime.exec(
            controller,
            "kubectl api-versions >/dev/null 2>&1; echo EXIT:$?",
            timeout=30.0,
        ).strip()
        if "EXIT:0" in output or output.splitlines()[-1:] == ["0"]:
            _progress(
                f"k3s API ready on {controller} after {time.time() - started:.0f}s"
            )
            return
        now = time.time()
        if now - last_log >= 30.0:
            _progress(
                f"waiting for k3s API on {controller} "
                f"({now - started:.0f}s / {_K3S_API_WAIT_SEC:.0f}s)"
            )
            last_log = now
        time.sleep(_K3S_API_POLL_SEC)
    raise TimeoutError(
        f"k3s API not ready on {controller!r} within {_K3S_API_WAIT_SEC}s"
    )


def mount_workload_cache(machine, scenario: str) -> None:
    """Mount the host image-cache directory read-only into k3s nodes."""
    if scenario not in K8S_SCENARIOS:
        return
    cache_root().mkdir(parents=True, exist_ok=True)
    machine.add_meta("volume", f"{cache_root()}|{_MOUNT_CACHE_DIR}|ro")


def import_tar_to_node(runtime: LabRuntime, node: str, tar_path: Path) -> None:
    remote_path = f"{_MOUNT_CACHE_DIR}/{tar_path.name}"
    # /bin/ctr is a k3s argv0 symlink; `k3s ctr` is not a valid subcommand on
    # rancher/k3s:v1.34.x ("No help topic for 'ctr'"). Kathara exec does not
    # raise on non-zero exit, so require an explicit marker.
    output = runtime.exec(
        node,
        f"if [ ! -f {remote_path} ]; then echo NIKA_IMPORT_MISSING; exit 1; fi; "
        f"ctr --address /run/k3s/containerd/containerd.sock -n k8s.io "
        f"images import {remote_path} >/tmp/nika-import.log 2>&1 "
        f"|| ctr -n k8s.io images import {remote_path} >/tmp/nika-import.log 2>&1; "
        f"echo NIKA_IMPORT_EXIT:$?; "
        f"tail -c 400 /tmp/nika-import.log 2>/dev/null || true",
        timeout=_IMPORT_TIMEOUT_SEC,
    )
    if "NIKA_IMPORT_MISSING" in output:
        raise RuntimeError(
            f"Cached image tar missing on {node}: {remote_path} "
            f"(is {_MOUNT_CACHE_DIR} mounted?)"
        )
    if "NIKA_IMPORT_EXIT:0" not in output:
        raise RuntimeError(
            f"Failed to import {tar_path.name} on {node}: {output.strip()[-500:]}"
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
    """Import cached workload images into k3s nodes before bootstrap applies manifests.

    Raises on failure. Does **not** write the preload signal unless imports succeed,
    so ``controller.startup`` will not apply MetalLB/apps against an empty containerd.
    """
    scenario = getattr(net_env, "LAB_NAME", None) or net_env.name
    if scenario not in K8S_SCENARIOS:
        return

    runtime = net_env._build_runtime()
    controller = "controller"
    cache_scenario(scenario)
    tar_paths = cached_tar_paths(scenario)
    expected = len(workload_images_for_scenario(scenario))
    if not tar_paths:
        raise RuntimeError(
            f"No workload images available for {scenario} after cache ensure "
            f"(host pull/save failed; check registry connectivity)"
        )
    if len(tar_paths) < expected:
        missing = [
            image
            for image in workload_images_for_scenario(scenario)
            if not cache_tar_exists(image)
        ]
        _progress(
            f"warning: {len(missing)} image(s) still uncached "
            f"(will try network pull inside cluster if reachable): "
            f"{', '.join(missing[:6])}{'…' if len(missing) > 6 else ''}"
        )

    _wait_k3s_api(runtime, controller)

    nodes = list(getattr(net_env, "kubernetes_nodes", []) or [])
    if not nodes:
        nodes = [name for name in runtime.list_nodes() if name.startswith("worker")]
        nodes.insert(0, controller)

    _progress(
        f"preloading {len(tar_paths)} image(s) × {len(nodes)} node(s) for {scenario}"
    )
    workers = min(_IMPORT_NODE_WORKERS, len(nodes))
    started = time.time()

    def import_node(node: str) -> None:
        try:
            _import_all_tars_to_node(runtime, node, tar_paths)
        except Exception as exc:
            raise RuntimeError(
                f"Workload image preload failed on {node}: {exc}"
            ) from exc

    bounded_parallel_map(import_node, nodes, max_workers=workers)

    signal_preload_complete(runtime, controller)
    _progress(
        f"preload complete for {scenario} "
        f"({len(tar_paths)} images × {len(nodes)} nodes in {time.time() - started:.0f}s)"
    )
