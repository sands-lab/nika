"""Install-time preparation and reconciliation of NIKA images and caches."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from docker.errors import APIError

from nika.config import REPO_ROOT
from nika.net_env.utils.kathara.docker_files.docker_images import (
    IMAGE_LABEL,
    LEGACY_NIKA_IMAGE_NAMES,
    NIKA_IMAGE_DOCKERFILES,
    _get_client,
    dockerfile_parents,
    ensure_nika_docker_images,
    host_machine_arch,
    image_repository,
)

VRNETLAB_BASE_IMAGE = "ghcr.io/srl-labs/vrnetlab-base:0.3.0@sha256:57f36ae1cf44a78a6b2cad35a6276565c56edfd28e8160ae9a772929db28fd6d"

# Repositories only NIKA uses: install prunes their local tags that the
# current release no longer references.
NIKA_ONLY_REPOS = frozenset(
    {
        "rancher/k3s",
        "nlnetlabs/routinator",
        "batfish/batfish",
        "ghcr.io/nokia/srlinux",
        "vrnetlab/mikrotik_routeros",
        "ios-xr/xrd-control-plane",
        "onosproject/onos",
    }
)
NIKA_ONLY_REPO_PREFIXES = (
    "nika/",
    "kathara/nika-",
    "quay.io/metallb/",
    "ghcr.io/llm-d/",
    "cr.agentgateway.dev/",
    "ik2227/",
)


def _strip_digest(ref: str) -> str:
    return ref.split("@", 1)[0]


def _tag_ref(ref: str) -> str:
    name = _strip_digest(ref)
    return name if ":" in name.rsplit("/", 1)[-1] else f"{name}:latest"


def _is_nika_only_repo(repository: str) -> bool:
    return repository in NIKA_ONLY_REPOS or repository.startswith(
        NIKA_ONLY_REPO_PREFIXES
    )


def _k8s_workload_images() -> list[str]:
    from nika.net_env.utils.k8s_workload_cache import (
        K8S_SCENARIOS,
        workload_images_for_scenario,
    )

    return sorted(
        {
            image
            for scenario in K8S_SCENARIOS
            for image in workload_images_for_scenario(scenario)
        }
    )


def runtime_images() -> list[str]:
    """Docker images any non-vendor scenario, failure, or k8s lab deploys."""
    from nika.net_env.isp.containerlab.lab import LINUX_IMAGE, SRL_IMAGE
    from nika.net_env.net_env_pool import list_all_net_envs
    from nika.net_env.utils.k8s_workload_cache import (
        K8S_SCENARIOS,
        host_images_for_scenario,
    )
    from nika.workflows.benchmark.release import collect_images_for_scenarios

    images = set(NIKA_IMAGE_DOCKERFILES)
    # k8s lab constructors stage Helm charts; their host images are static.
    # Licensed vendor images are prepared by install.sh --with-vendor-images.
    images.update(
        collect_images_for_scenarios(
            {
                name
                for name, spec in list_all_net_envs(backend="kathara").items()
                if not spec.licensed_images and name not in K8S_SCENARIOS
            }
        )
    )
    for scenario in K8S_SCENARIOS:
        images.update(host_images_for_scenario(scenario))
    images.update({SRL_IMAGE, LINUX_IMAGE})
    return sorted(images)


def build_parent_images() -> list[str]:
    """External ``FROM`` images pulled to build local nika/* images."""
    return sorted(
        {
            parent
            for image in NIKA_IMAGE_DOCKERFILES
            for parent in dockerfile_parents(image)
            if parent not in NIKA_IMAGE_DOCKERFILES
        }
    )


def owned_images() -> list[str]:
    """Every Docker image the current NIKA release may create or pull."""
    from nika.net_env.routeros_simple_bgp.lab import IMAGE as ROUTEROS_IMAGE
    from nika.net_env.utils.iosxr.common import IMAGE as XRD_IMAGE
    from nika.validation.batfish.service import BATFISH_IMAGE

    images = set(runtime_images()) | set(build_parent_images())
    images.update(
        {
            ROUTEROS_IMAGE,
            f"{ROUTEROS_IMAGE}-{host_machine_arch()}",
            XRD_IMAGE,
            VRNETLAB_BASE_IMAGE,
            BATFISH_IMAGE,
        }
    )
    return sorted(images)


def legacy_images() -> list[str]:
    """Tags created by older NIKA releases that the current layout replaces.

    Includes pre-rename ``kathara/nika-*`` tags and the Docker copies of k8s
    workload images, which now live only in ``.nika_cache/k8s-images``.
    Docker Official Images (no namespace, e.g. ``postgres``) are excluded.
    """
    legacy = {name for names in LEGACY_NIKA_IMAGE_NAMES.values() for name in names}
    legacy.update(_tag_ref(image) for image in _k8s_workload_images() if "/" in image)
    return sorted(legacy)


def prune_stale_images() -> list[str]:
    """Remove superseded NIKA image tags and dangling NIKA builds.

    Only touches NIKA-only repositories, legacy NIKA tags, and dangling images
    labeled by a NIKA build. Images used by any container are kept.
    """
    client = _get_client()
    owned = owned_images()
    keep_tags = {_tag_ref(ref) for ref in owned if "@" not in ref}
    keep_digests = {
        f"{image_repository(ref)}@{ref.split('@', 1)[1]}" for ref in owned if "@" in ref
    }
    legacy = set(legacy_images())
    in_use = {
        container.attrs.get("Image")
        for container in client.containers.list(all=True, ignore_removed=True)
    }
    actions: list[str] = []

    def remove(ref: str, image_id: str) -> None:
        if image_id in in_use:
            actions.append(f"kept {ref} (used by a container)")
            return
        try:
            client.images.remove(ref)
        except APIError as exc:
            actions.append(f"kept {ref} ({exc.explanation or exc})")
            return
        actions.append(f"removed stale image {ref}")

    for image in client.images.list():
        tags = list(image.tags)
        digests = set(image.attrs.get("RepoDigests") or [])
        kept = bool(set(tags) & keep_tags or digests & keep_digests)
        stale = [
            tag
            for tag in tags
            if tag not in keep_tags
            and (tag in legacy or _is_nika_only_repo(image_repository(tag)))
        ]
        if not tags and not kept and digests:
            if all(_is_nika_only_repo(image_repository(ref)) for ref in digests):
                remove(sorted(digests)[0], image.id)
            continue
        remaining = len(tags)
        for tag in stale:
            # Untagging the last tag of a kept image would delete it.
            if kept and remaining <= 1:
                continue
            remove(tag, image.id)
            remaining -= 1

    for image in client.images.list(filters={"dangling": True, "label": IMAGE_LABEL}):
        remove(image.id, image.id)
    return actions


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def prune_stale_caches() -> list[str]:
    """Remove ``.nika_cache`` entries the current release no longer uses."""
    from nika.net_env.llmd_lab.lab import (
        _AGENTGATEWAY_VERSION,
        _HELM_CHART_SPECS,
        _HELM_VERSION,
    )
    from nika.net_env.routeros_simple_bgp.lab import IMAGE as ROUTEROS_IMAGE
    from nika.net_env.utils.k8s_workload_cache import cache_root, cache_tar_path

    actions: list[str] = []

    def prune(directory: Path, keep: set[str], label: str) -> None:
        if not directory.is_dir():
            return
        for entry in sorted(directory.iterdir()):
            if entry.name not in keep:
                _remove_path(entry)
                actions.append(f"removed stale {label} {entry.name}")

    prune(
        cache_root(),
        {cache_tar_path(image).name for image in _k8s_workload_images()},
        "k8s image archive",
    )
    helm_root = REPO_ROOT / ".nika_cache" / "helm"
    prune(helm_root, {_HELM_VERSION, "charts"}, "Helm cache")
    prune(
        helm_root / "charts",
        {f"{name}-{_AGENTGATEWAY_VERSION}.tgz" for name, _ in _HELM_CHART_SPECS},
        "Helm chart",
    )

    routeros_version = ROUTEROS_IMAGE.rsplit(":", 1)[1]
    vendor = Path(
        os.environ.get("NIKA_VENDOR_CACHE") or REPO_ROOT / ".nika_cache" / "vendor"
    )
    if vendor.is_dir():
        for entry in sorted(vendor.glob("chr-*")):
            current = entry.name.startswith(
                (f"chr-{routeros_version}.", f"chr-{routeros_version}-")
            )
            if not current or entry.name.endswith(".partial"):
                _remove_path(entry)
                actions.append(f"removed stale vendor download {entry.name}")
    return actions


def prepare_all_images(*, force_rebuild: bool = False) -> None:
    """Reconcile, then build, pull, and cache everything benchmarks deploy."""
    from nika.net_env.utils.k8s_workload_cache import K8S_SCENARIOS, cache_scenario
    from agent.sandbox.sbx.images import ensure_configured_sbx_template_images

    # Images are pruned only after ensure: it retags identical builds from
    # other NIKA versions (e.g. nika/base:latest) instead of rebuilding them.
    for action in prune_stale_caches():
        print(action)
    ensure_nika_docker_images(runtime_images(), force_rebuild=force_rebuild)
    for scenario in sorted(K8S_SCENARIOS):
        cache_scenario(scenario)
    ensure_configured_sbx_template_images()
    actions = prune_stale_images()
    for action in actions:
        print(action)
    if not actions:
        print("No stale NIKA images")
