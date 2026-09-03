"""Build, pull, and verify local NIKA Docker images via the Docker Python API."""

from __future__ import annotations

import platform
from pathlib import Path
from typing import Iterable, Set

import docker
from docker.errors import APIError, BuildError, ImageNotFound

NIKA_IMAGE_PREFIX = "nika/"
DOCKER_FILES_DIR = Path(__file__).resolve().parent

# Scenario-local images required at deploy time. Upstream Kathara images
# (kathara/base, kathara/frr, …) are pulled, not listed here.
NIKA_IMAGE_DOCKERFILES: dict[str, str] = {
    "nika/frr": "Dockerfile.frr",
    "nika/base": "Dockerfile.base",
    "nika/nginx": "Dockerfile.nginx",
    "nika/wireguard": "Dockerfile.wireguard",
    "nika/pox": "Dockerfile.pox",
    "nika/onos": "Dockerfile.onos",
    "nika/fabric-controller": "Dockerfile.fabric-controller",
    "nika/routinator:v0.14.2": "../isp/rpki/Dockerfile.routinator",
}

# Images whose upstream base (or binaries) are single-arch. Builds and pulls
# must target this platform so arm64 hosts do not produce mixed-arch layers.
NIKA_IMAGE_PLATFORMS: dict[str, str] = {
    "nika/onos": "linux/amd64",
}

# Old tags from before the nika/* rename. ensure retags these when the new
# name is missing so local builds are not repeated.
LEGACY_NIKA_IMAGE_NAMES: dict[str, tuple[str, ...]] = {
    "nika/base": ("kathara/nika-base",),
    "nika/frr": ("kathara/nika-frr",),
    "nika/nginx": ("kathara/nika-nginx",),
    "nika/wireguard": ("kathara/nika-wireguard",),
    "nika/pox": ("kathara/nika-pox",),
}

_QEMU_X86_64_BINFMT = Path("/proc/sys/fs/binfmt_misc/qemu-x86_64")

_client: docker.DockerClient | None = None


def _get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def image_exists(image: str) -> bool:
    try:
        _get_client().images.get(image)
    except ImageNotFound:
        return False
    return True


def _dockerfile_for_image(image: str) -> Path:
    dockerfile_name = NIKA_IMAGE_DOCKERFILES.get(image)
    if dockerfile_name is None:
        suffix = image.removeprefix(NIKA_IMAGE_PREFIX)
        dockerfile_name = f"Dockerfile.{suffix}"
    dockerfile = (DOCKER_FILES_DIR / dockerfile_name).resolve()
    if not dockerfile.is_file():
        raise FileNotFoundError(f"No Dockerfile for image {image}: {dockerfile}")
    return dockerfile


def _is_locally_buildable(image: str) -> bool:
    if image in NIKA_IMAGE_DOCKERFILES:
        return True
    if not image.startswith(NIKA_IMAGE_PREFIX):
        return False
    try:
        _dockerfile_for_image(image)
        return True
    except FileNotFoundError:
        return False


def _split_image_tag(image: str) -> tuple[str, str | None]:
    if ":" in image:
        repo, tag = image.rsplit(":", 1)
        return repo, tag
    return image, None


def _platform_for_image(image: str) -> str | None:
    return NIKA_IMAGE_PLATFORMS.get(image)


def _arch_from_platform(docker_platform: str) -> str:
    # linux/amd64 -> amd64; linux/arm64/v8 -> arm64
    parts = docker_platform.split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid Docker platform: {docker_platform}")
    return parts[1]


def host_machine_arch() -> str:
    """Return a Docker-style CPU arch for the host (amd64 / arm64 / …)."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    return machine


def host_can_run_amd64() -> bool:
    """Whether this host can execute linux/amd64 container binaries.

    Darwin (Docker Desktop / Rosetta) is treated as capable, matching Kathara.
    Linux arm64 requires qemu-x86_64 binfmt registration.
    """
    arch = host_machine_arch()
    if arch == "amd64":
        return True
    if platform.system() == "Darwin":
        return True
    return _QEMU_X86_64_BINFMT.is_file()


def _require_platform_support(image: str, docker_platform: str) -> None:
    target_arch = _arch_from_platform(docker_platform)
    if target_arch == "amd64" and not host_can_run_amd64():
        raise RuntimeError(
            f"Docker image {image} requires platform {docker_platform}, but this "
            f"host ({platform.system()} {host_machine_arch()}) cannot run amd64 "
            "containers. On Linux arm64, install qemu-user-static / binfmt "
            "(e.g. qemu-x86_64 under /proc/sys/fs/binfmt_misc) so Docker can "
            "emulate amd64; otherwise use an amd64 host or Docker Desktop on Mac."
        )


def image_architecture(image: str) -> str | None:
    """Return the local image Architecture attribute, or None if missing."""
    try:
        img = _get_client().images.get(image)
    except ImageNotFound:
        return None
    return img.attrs.get("Architecture")


def _assert_image_architecture(image: str, expected_arch: str) -> None:
    actual = image_architecture(image)
    if actual != expected_arch:
        raise RuntimeError(
            f"Docker image {image} Architecture is {actual!r}, expected "
            f"{expected_arch!r}. Rebuild with platform forcing "
            f"({NIKA_IMAGE_PLATFORMS.get(image) or expected_arch})."
        )


def retag_image(source: str, target: str) -> None:
    """Copy ``source`` to ``target`` and remove the ``source`` name (rename)."""
    print(f"Renaming Docker image {source} -> {target}...")
    client = _get_client()
    try:
        img = client.images.get(source)
    except ImageNotFound as exc:
        raise RuntimeError(f"Legacy Docker image not found: {source}") from exc

    repo, tag = _split_image_tag(target)
    if not img.tag(repo, tag=tag):
        raise RuntimeError(f"Failed to tag Docker image {source} as {target}")

    try:
        client.images.remove(source)
    except APIError as exc:
        raise RuntimeError(f"Failed to remove legacy Docker image {source}") from exc


def _migrate_legacy_image(image: str) -> bool:
    """If a legacy tag exists for ``image``, retag it. Return True if migrated."""
    for legacy in LEGACY_NIKA_IMAGE_NAMES.get(image, ()):
        if image_exists(legacy):
            retag_image(legacy, image)
            return True
    return False


def build_nika_image(image: str) -> None:
    dockerfile = _dockerfile_for_image(image)
    docker_platform = _platform_for_image(image)
    if docker_platform:
        _require_platform_support(image, docker_platform)
        print(
            f"Building Docker image {image} from {dockerfile.name} "
            f"(platform={docker_platform})..."
        )
    else:
        print(f"Building Docker image {image} from {dockerfile.name}...")

    build_kwargs: dict = {
        "path": str(dockerfile.parent),
        "dockerfile": dockerfile.name,
        "tag": image,
        "network_mode": "host",
        "rm": True,
    }
    if docker_platform:
        build_kwargs["platform"] = docker_platform

    try:
        _, build_log = _get_client().images.build(**build_kwargs)
        for chunk in build_log:
            if "stream" in chunk:
                print(chunk["stream"], end="")
            elif "error" in chunk:
                raise BuildError(chunk["error"], build_log)
    except BuildError as exc:
        raise RuntimeError(f"Failed to build Docker image {image}") from exc

    if docker_platform:
        _assert_image_architecture(image, _arch_from_platform(docker_platform))


def pull_image(image: str, *, platform: str | None = None) -> None:
    docker_platform = platform if platform is not None else _platform_for_image(image)
    if docker_platform:
        _require_platform_support(image, docker_platform)
        print(f"Pulling Docker image {image} (platform={docker_platform})...")
    else:
        print(f"Pulling Docker image {image}...")
    try:
        if docker_platform:
            _get_client().images.pull(image, platform=docker_platform)
        else:
            _get_client().images.pull(image)
    except APIError as exc:
        raise RuntimeError(f"Failed to pull Docker image {image}") from exc

    if docker_platform:
        _assert_image_architecture(image, _arch_from_platform(docker_platform))


def ensure_nika_docker_images(
    required_images: Iterable[str], *, force_rebuild: bool = False
) -> None:
    """Ensure required images are available locally.

    Locally buildable ``nika/*`` images are built when missing, after first
    checking for and renaming legacy ``kathara/nika-*`` tags. Other images
    (e.g. upstream ``kathara/p4``) are pulled. With
    ``force_rebuild=True``, every buildable image is rebuilt; pullable images
    are still only fetched when missing.

    Images listed in ``NIKA_IMAGE_PLATFORMS`` are built/pulled for that
    platform (e.g. ``nika/onos`` → ``linux/amd64``).
    """
    required = {img for img in required_images if img}
    if not required:
        return

    buildable = {img for img in required if _is_locally_buildable(img)}
    pullable = required - buildable

    if force_rebuild:
        to_build = buildable
    else:
        missing_buildable = {img for img in buildable if not image_exists(img)}
        to_build = set()
        for image in sorted(missing_buildable):
            if not _migrate_legacy_image(image):
                to_build.add(image)

    to_pull = {img for img in pullable if not image_exists(img)}

    if to_build:
        if force_rebuild:
            print(f"Force rebuilding Docker images: {', '.join(sorted(to_build))}")
        else:
            print(f"Missing Docker images (build): {', '.join(sorted(to_build))}")
        for image in sorted(to_build):
            build_nika_image(image)

    if to_pull:
        print(f"Missing Docker images (pull): {', '.join(sorted(to_pull))}")
        for image in sorted(to_pull):
            pull_image(image)

    still_missing: Set[str] = {img for img in required if not image_exists(img)}
    if still_missing:
        raise RuntimeError(
            "Failed to ensure required Docker images: "
            + ", ".join(sorted(still_missing))
        )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build NIKA Docker images.")
    parser.add_argument(
        "-f",
        "--force-rebuild",
        action="store_true",
        help="Rebuild images even if they already exist locally.",
    )
    parser.add_argument(
        "images",
        nargs="*",
        metavar="IMAGE",
        help="Images to build (default: all known nika/* images).",
    )
    args = parser.parse_args()
    required = args.images or list(NIKA_IMAGE_DOCKERFILES.keys())
    ensure_nika_docker_images(required, force_rebuild=args.force_rebuild)


if __name__ == "__main__":
    main()
