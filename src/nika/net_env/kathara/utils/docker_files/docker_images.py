"""Build, pull, and verify local NIKA Kathara Docker images via the Docker Python API."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Set

import docker
from docker.errors import APIError, BuildError, ImageNotFound

NIKA_IMAGE_PREFIX = "kathara/nika-"
DOCKER_FILES_DIR = Path(__file__).resolve().parent

# Mirrors src/nika/net_env/kathara/utils/DockerFiles/build_dockers.sh, plus
# scenario-local images that are required at deploy time.
NIKA_IMAGE_DOCKERFILES: dict[str, str] = {
    "kathara/nika-frr": "Dockerfile.frr",
    "kathara/nika-base": "Dockerfile.base",
    "kathara/nika-nginx": "Dockerfile.nginx",
    "kathara/nika-wireguard": "Dockerfile.wireguard",
    "kathara/nika-pox": "Dockerfile.pox",
    "kathara/influxdb": "../../p4/p4_int/Dockerfile",
}

_client: docker.DockerClient | None = None


def _get_client() -> docker.DockerClient:
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


def image_exists(image: str) -> bool:
    try:
        _get_client().images.get(image)
        return True
    except ImageNotFound:
        return False


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


def build_nika_image(image: str) -> None:
    dockerfile = _dockerfile_for_image(image)
    print(f"Building Docker image {image} from {dockerfile.name}...")
    try:
        _, build_log = _get_client().images.build(
            path=str(dockerfile.parent),
            dockerfile=dockerfile.name,
            tag=image,
            network_mode="host",
            rm=True,
        )
        for chunk in build_log:
            if "stream" in chunk:
                print(chunk["stream"], end="")
            elif "error" in chunk:
                raise BuildError(chunk["error"], build_log)
    except BuildError as exc:
        raise RuntimeError(f"Failed to build Docker image {image}") from exc


def pull_image(image: str) -> None:
    print(f"Pulling Docker image {image}...")
    try:
        _get_client().images.pull(image)
    except APIError as exc:
        raise RuntimeError(f"Failed to pull Docker image {image}") from exc


def ensure_nika_docker_images(
    required_images: Iterable[str], *, force_rebuild: bool = False
) -> None:
    """Ensure required images are available locally.

    Locally buildable ``kathara/nika-*`` (and mapped) images are built when
    missing. Other images (e.g. upstream ``kathara/p4``) are pulled. With
    ``force_rebuild=True``, every buildable image is rebuilt; pullable images
    are still only fetched when missing.
    """
    required = {img for img in required_images if img}
    if not required:
        return

    buildable = {img for img in required if _is_locally_buildable(img)}
    pullable = required - buildable

    if force_rebuild:
        to_build = buildable
    else:
        to_build = {img for img in buildable if not image_exists(img)}
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

    parser = argparse.ArgumentParser(description="Build NIKA Kathara Docker images.")
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
        help="Images to build (default: all known kathara/nika-* images).",
    )
    args = parser.parse_args()
    required = args.images or list(NIKA_IMAGE_DOCKERFILES.keys())
    ensure_nika_docker_images(required, force_rebuild=args.force_rebuild)


if __name__ == "__main__":
    main()
