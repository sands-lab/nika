"""Host-side cache and k3s sideload for Kubernetes lab workload images."""

from __future__ import annotations

import hashlib
import io
import json
import re
import os
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING

import docker.errors
import requests

from nika.config import REPO_ROOT
from nika.net_env.utils.kathara.docker_files.docker_images import (
    _get_client,
    ensure_nika_docker_images,
    host_machine_arch,
)
from nika.net_env.net_env_pool import list_all_net_envs

if TYPE_CHECKING:
    from nika.net_env.base import NetworkEnvBase
    from nika.runtime.base import LabRuntime

K3S_IMAGE = "rancher/k3s:v1.34.1-k3s1@sha256:5e0707cfd1239b358ef73f3254bc3eadc027dd30cd5ec6ca41e29e47652a1b8c"

# System images used by rancher/k3s:v1.34.1-k3s1.
K3S_SYSTEM_IMAGES = (
    "rancher/mirrored-pause:3.6@sha256:74c4244427b7312c5b901fe0f67cbc53683d06f4f24c6faee65d4182bf0fa893",
    "rancher/mirrored-coredns-coredns:1.12.3@sha256:1391544c978029fcddc65068f6ad67f396e55585b664ecccd7fefba029b9b706",
    "rancher/mirrored-metrics-server:v0.8.0@sha256:89258156d0e9af60403eafd44da9676fd66f600c7934d468ccc17e42b199aee2",
    "rancher/local-path-provisioner:v0.0.32@sha256:9289da488b07912cb4128eb96928a331a5f3e60c28c5cfc5790f354a4ad0cc68",
)

K8S_SCENARIOS = frozenset(
    name for name, spec in list_all_net_envs().items() if spec.k8s_image_cache
)

SKOPEO_IMAGE = "nika/skopeo"
_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

_PRELOAD_SIGNAL_PATH = "/var/run/nika-images-preloaded"
_MOUNT_CACHE_DIR = "/nika-image-cache"
_K3S_API_WAIT_SEC = 600.0
_K3S_API_POLL_SEC = 2.0
_IMPORT_TIMEOUT_SEC = 600.0


def cache_root() -> Path:
    return REPO_ROOT / ".nika_cache" / "k8s-images"


def _k8s_scenario_class(scenario: str) -> type["NetworkEnvBase"] | None:
    if scenario not in K8S_SCENARIOS:
        return None
    from nika.net_env.net_env_pool import _load_net_env_class

    return _load_net_env_class(scenario, backend="kathara")


def workload_images_for_scenario(scenario: str) -> tuple[str, ...]:
    cls = _k8s_scenario_class(scenario)
    return tuple(cls.K8S_WORKLOAD_IMAGES) if cls is not None else ()


def host_images_for_scenario(scenario: str) -> tuple[str, ...]:
    cls = _k8s_scenario_class(scenario)
    return tuple(cls.K8S_HOST_IMAGES) if cls is not None else ()


def cache_tar_path(image: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "__", image)
    return cache_root() / f"{safe}__linux-{host_machine_arch()}.tar"


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


def _tar_is_complete(path: Path, image: str | None = None) -> bool:
    """Validate the pinned OCI graph for the host platform, including blob hashes."""
    try:
        with tarfile.open(path) as archive:
            index = json.load(archive.extractfile("index.json"))
            roots = index["manifests"]
            if not roots or (image and roots[0]["digest"] != image.split("@")[1]):
                return False
            visited: set[str] = set()

            def validate(descriptor: dict) -> None:
                digest = descriptor["digest"]
                if digest in visited:
                    return
                algorithm, value = digest.split(":", 1)
                if algorithm != "sha256":
                    raise ValueError("Only SHA256 image digests are supported")
                member = archive.getmember(f"blobs/sha256/{value}")
                if member.size != descriptor["size"]:
                    raise ValueError("Blob size does not match descriptor")
                stream = archive.extractfile(member)
                if hashlib.file_digest(stream, "sha256").hexdigest() != value:
                    raise ValueError("Blob digest does not match content")
                visited.add(digest)
                if (
                    "manifest" in descriptor["mediaType"]
                    or "index" in descriptor["mediaType"]
                ):
                    document = json.load(archive.extractfile(member))
                    children = document.get("manifests", [])
                    children = [
                        child
                        for child in children
                        if not child.get("platform")
                        or (
                            child["platform"].get("os") == "linux"
                            and child["platform"].get("architecture")
                            == host_machine_arch()
                        )
                    ]
                    if "manifests" in document and not children:
                        raise ValueError("No image for the host platform")
                    for child in children:
                        validate(child)
                    if "config" in document:
                        validate(document["config"])
                        config_path = document["config"]["digest"].replace(":", "/", 1)
                        config = json.load(archive.extractfile("blobs/" + config_path))
                        if (
                            config.get("os") != "linux"
                            or config.get("architecture") != host_machine_arch()
                        ):
                            raise ValueError(
                                "Image configuration does not support the host platform"
                            )
                    for child in document.get("layers", []):
                        validate(child)

            for root in roots:
                validate(root)
        return True
    except (OSError, tarfile.TarError, KeyError, TypeError, AttributeError, ValueError):
        return False


def _write_oci_archive(directory: Path, archive_path: Path, image: str) -> None:
    """Package skopeo's dir transport without converting upstream manifests.

    The OCI transport converts Docker manifest lists, changing their digests.
    Preserve the original graph so Kubernetes digest references resolve offline.
    """
    manifest = (directory / "manifest.json").read_bytes()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    if digest != image.split("@")[1]:
        raise RuntimeError(f"Registry returned an unexpected manifest for {image}")
    name = image.split("@")[0]
    if "/" not in name:
        name = "docker.io/library/" + name
    elif "." not in name.split("/")[0] and ":" not in name.split("/")[0]:
        name = "docker.io/" + name
    descriptor = {
        "mediaType": json.loads(manifest)["mediaType"],
        "digest": digest,
        "size": len(manifest),
        "annotations": {"io.containerd.image.name": name},
    }
    digest_reference = name.rsplit(":", 1)[0] + "@" + digest
    digest_descriptor = dict(
        descriptor, annotations={"io.containerd.image.name": digest_reference}
    )
    with tarfile.open(archive_path, "w") as archive:
        for name, data in {
            "oci-layout": b'{"imageLayoutVersion":"1.0.0"}',
            "index.json": json.dumps(
                {"schemaVersion": 2, "manifests": [descriptor, digest_descriptor]}
            ).encode(),
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for blob in directory.iterdir():
            if blob.name == "version":
                continue
            value = (
                digest.split(":")[1]
                if blob.name == "manifest.json"
                else blob.name.removesuffix(".manifest.json")
            )
            archive.add(blob, arcname=f"blobs/sha256/{value}", recursive=False)


def _registry_auth() -> dict:
    """Inline ``docker login`` credentials; credential helpers stay on the host."""
    try:
        config = json.loads((Path.home() / ".docker" / "config.json").read_text())
    except (OSError, ValueError):
        return {}
    auths = config.get("auths") if isinstance(config, dict) else None
    if not isinstance(auths, dict):
        return {}
    return {
        registry: {"auth": entry["auth"]}
        for registry, entry in auths.items()
        if isinstance(entry, dict) and entry.get("auth")
    }


def _skopeo(*args: str, mount: Path, timeout: float) -> bytes:
    """Run skopeo from ``nika/skopeo`` as the host user, sharing ``mount``.

    Host networking, proxy settings, and inline ``docker login`` credentials
    match a host-installed skopeo.
    """
    environment = {"TMPDIR": str(mount)}
    environment.update(
        {key: os.environ[key] for key in _PROXY_ENV_KEYS if key in os.environ}
    )
    auths = _registry_auth()
    if auths:
        auth_file = mount / "auth.json"
        auth_file.touch(mode=0o600)
        auth_file.write_text(json.dumps({"auths": auths}))
        environment["REGISTRY_AUTH_FILE"] = str(auth_file)
    try:
        container = _get_client().containers.create(
            SKOPEO_IMAGE,
            ["skopeo", *args],
            user=f"{os.getuid()}:{os.getgid()}",
            network_mode="host",
            environment=environment,
            volumes={str(mount): {"bind": str(mount), "mode": "rw"}},
        )
    except docker.errors.DockerException as exc:
        raise RuntimeError(f"could not create the skopeo container: {exc}") from exc
    try:
        container.start()
        try:
            status = container.wait(timeout=timeout).get("StatusCode", 1)
        except requests.exceptions.RequestException as exc:
            raise TimeoutError(
                f"skopeo {args[0]} did not finish within {timeout:.0f}s "
                f"or lost the Docker connection: {exc}"
            ) from exc
        if status:
            stderr = container.logs(stdout=False, stderr=True).decode(errors="replace")
            raise RuntimeError(stderr.strip()[-2000:])
        return container.logs(stdout=True, stderr=False)
    except docker.errors.DockerException as exc:
        raise RuntimeError(f"skopeo {args[0]} failed in Docker: {exc}") from exc
    finally:
        container.remove(force=True)


def ensure_cached(image: str) -> Path:
    """Fetch a digest-pinned, complete image graph independently of Docker's store."""
    if "@sha256:" not in image:
        raise ValueError(f"Workload image must be pinned by digest: {image}")
    tar_path = cache_tar_path(image)
    if cache_tar_exists(image) and _tar_is_complete(tar_path, image):
        return tar_path
    ensure_nika_docker_images([SKOPEO_IMAGE])
    cache_root().mkdir(parents=True, exist_ok=True)
    _progress(f"fetching {image}")
    repository, digest = image.split("@", 1)
    source = (
        (
            repository.rsplit(":", 1)[0]
            if ":" in repository.rsplit("/", 1)[-1]
            else repository
        )
        + "@"
        + digest
    )
    with tempfile.TemporaryDirectory(dir=cache_root()) as tmp:
        directory = Path(tmp) / "image"
        staged = Path(tmp) / "image.tar"
        try:
            _skopeo(
                "copy",
                "--override-os",
                "linux",
                "--override-arch",
                host_machine_arch(),
                "--preserve-digests",
                "--retry-times",
                "2",
                f"docker://{source}",
                f"dir:{directory}",
                mount=Path(tmp),
                timeout=600,
            )
            # Keep the upstream index identity while including only the selected
            # platform's blobs. ctr --platform resolves this sparse index offline.
            root = _skopeo(
                "inspect", "--raw", f"docker://{source}", mount=Path(tmp), timeout=60
            )
            selected = directory / "manifest.json"
            selected.replace(
                directory
                / (hashlib.sha256(selected.read_bytes()).hexdigest() + ".manifest.json")
            )
            selected.write_bytes(root)
        except TimeoutError as exc:
            raise RuntimeError(f"Image preparation stopped: {image}: {exc}") from exc
        except RuntimeError as exc:
            raise RuntimeError(f"Could not cache {image}: {exc}") from exc
        _write_oci_archive(directory, staged, image)
        if not _tar_is_complete(staged, image):
            raise RuntimeError(f"Incomplete or corrupt image archive for {image}")
        staged.chmod(0o644)
        staged.replace(tar_path)
    return tar_path


def ensure_workload_cache(scenario: str) -> list[Path]:
    """Ensure host tar caches exist for a scenario's workload images."""
    images = workload_images_for_scenario(scenario)
    already = sum(1 for image in images if cache_tar_exists(image))
    cached: list[Path] = []
    for image in images:
        tar_path = ensure_cached(image)
        cached.append(tar_path)
    # Retire only the known tag-only archives after their replacements validate.
    for image in images:
        legacy_name = re.sub(r"[^A-Za-z0-9._-]+", "__", image.split("@")[0])
        (cache_root() / f"{legacy_name}.tar").unlink(missing_ok=True)
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
    cls = _k8s_scenario_class(scenario)
    if cls is not None:
        cls.prepare_k8s_image_cache()


def cached_tar_paths(scenario: str) -> list[Path]:
    return [
        cache_tar_path(image)
        for image in workload_images_for_scenario(scenario)
        if cache_tar_exists(image)
    ]


def _wait_k3s_api(
    runtime: LabRuntime,
    controller: str = "controller",
    *,
    nodes: list[str] | None = None,
) -> None:
    from nika.net_env.verify import raise_for_k8s_startup_failure

    deadline = time.time() + _K3S_API_WAIT_SEC
    containers = {node: runtime.get_container(node) for node in nodes or [controller]}
    started = time.time()
    last_log = 0.0
    while time.time() < deadline:
        raise_for_k8s_startup_failure(runtime, containers)
        output = runtime.exec(
            controller,
            "kubectl --request-timeout=5s get --raw=/readyz >/dev/null 2>&1; echo EXIT:$?",
            timeout=30.0,
        ).strip()
        if "EXIT:0" in output or output.splitlines()[-1:] == ["0"]:
            if nodes:
                from nika.net_env.verify import k8s_ready_node_count

                ready = runtime.exec(
                    controller,
                    "kubectl --request-timeout=5s get nodes --no-headers",
                    timeout=15,
                )
                if k8s_ready_node_count(ready) < len(nodes):
                    time.sleep(_K3S_API_POLL_SEC)
                    continue
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
    """Stage the image cache and block node registry fallback, including system pods."""
    if scenario not in K8S_SCENARIOS:
        return
    cache_root().mkdir(parents=True, exist_ok=True)
    machine.add_meta("volume", f"{cache_root()}|{_MOUNT_CACHE_DIR}|ro")
    # A refused local endpoint prevents mutable system-image tags from being
    # fetched before host preload. Empty mirror entries are discarded by k3s.
    machine.create_file_from_string(
        'mirrors:\n  "*":\n    endpoint:\n    - "http://127.0.0.1:1"\n',
        "/etc/rancher/k3s/registries.yaml",
    )


def import_tar_to_node(runtime: LabRuntime, node: str, tar_path: Path) -> None:
    remote_path = f"{_MOUNT_CACHE_DIR}/{tar_path.name}"
    # /bin/ctr is a k3s argv0 symlink; `k3s ctr` is not a valid subcommand on
    # rancher/k3s:v1.34.x ("No help topic for 'ctr'"). Kathara exec does not
    # raise on non-zero exit, so require an explicit marker.
    output = runtime.exec(
        node,
        f"if [ ! -f {remote_path} ]; then echo NIKA_IMPORT_MISSING; exit 1; fi; "
        f"ctr --address /run/k3s/containerd/containerd.sock -n k8s.io "
        f"images import --local --platform linux/{host_machine_arch()} {remote_path} >/tmp/nika-import.log 2>&1; "
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


def preload_workload_images(net_env: NetworkEnvBase) -> None:
    """Import cached workload images into k3s nodes before bootstrap applies manifests.

    Raises on failure. Does **not** write the preload signal unless imports succeed,
    so ``controller.startup`` will not apply MetalLB/apps against an empty containerd.
    """
    from nika.net_env.verify import raise_for_k8s_startup_failure

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
            f"(image preparation failed; check registry connectivity)"
        )
    if len(tar_paths) < expected:
        missing = [
            image
            for image in workload_images_for_scenario(scenario)
            if not cache_tar_exists(image)
        ]
        raise RuntimeError(
            f"Workload image cache is incomplete for {scenario}: {missing}"
        )

    nodes = list(getattr(net_env, "kubernetes_nodes", []) or [])
    if not nodes:
        nodes = [name for name in runtime.list_nodes() if name.startswith("worker")]
        nodes.insert(0, controller)

    _wait_k3s_api(runtime, controller, nodes=nodes)
    containers = {node: runtime.get_container(node) for node in nodes}

    _progress(
        f"preloading {len(tar_paths)} image(s) × {len(nodes)} node(s) for {scenario}"
    )
    started = time.time()

    # Serial imports avoid Kathara exec races and containerd socket contention.
    for node in nodes:
        # Keep the Docker object before k3s can exit; Kathara may no longer
        # resolve a stopped machine when the failure handler inspects it.
        container = containers[node]
        _progress(f"importing images on {node} for {scenario}")
        for tar_path in tar_paths:
            try:
                raise_for_k8s_startup_failure(runtime, containers)
                import_tar_to_node(runtime, node, tar_path)
            except Exception as exc:
                details: dict = {
                    "scenario": scenario,
                    "lab_name": net_env.name,
                    "node": node,
                    "image_tar": tar_path.name,
                    "error": str(exc),
                }
                try:
                    details["container_id"] = container.id
                    container.reload()
                    details["container_state"] = container.attrs.get("State", {})
                    logs = container.logs(tail=100, timestamps=True)
                    details["container_logs_tail"] = logs.decode(errors="replace")[
                        -8192:
                    ]
                except Exception as diagnostics_exc:  # noqa: BLE001
                    details["container_diagnostics_error"] = str(diagnostics_exc)
                try:
                    from nika.utils.logger import log_error_event

                    log_error_event(
                        "env_preload_node_failed",
                        f"Workload image preload failed on {node} while importing "
                        f"{tar_path.name}: {exc}",
                        **details,
                    )
                except Exception:  # noqa: BLE001 - preserve the import failure
                    pass
                raise RuntimeError(
                    f"Workload image preload failed on {node} while importing "
                    f"{tar_path.name}: {exc}"
                ) from exc
        _progress(f"images ready on {node} for {scenario}")

    runtime.exec(
        controller,
        f"mkdir -p /var/run && touch {_PRELOAD_SIGNAL_PATH}",
        timeout=15.0,
    )
    _progress(
        f"preload complete for {scenario} "
        f"({len(tar_paths)} images × {len(nodes)} nodes in {time.time() - started:.0f}s)"
    )
