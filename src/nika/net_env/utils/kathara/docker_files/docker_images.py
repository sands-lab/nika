"""Build, pull, and verify local NIKA Docker images via the Docker Python API."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
from pathlib import Path
from typing import Iterable, Set

import docker
from docker.errors import APIError, BuildError, ImageNotFound

from nika.config import BENCHMARK_VERSION

NIKA_IMAGE_PREFIX = "nika/"
DOCKER_FILES_DIR = Path(__file__).resolve().parent

# Locally built nika/* images carry the benchmark version as their tag.
# Dockerfiles reference other nika/* parents through this build arg.
NIKA_IMAGE_TAG = BENCHMARK_VERSION
NIKA_IMAGE_TAG_ARG = "NIKA_IMAGE_TAG"
_BUILD_ARGS = {NIKA_IMAGE_TAG_ARG: NIKA_IMAGE_TAG}

# Labels on locally built nika/* images. The build hash covers the Dockerfile,
# its COPY/ADD sources, and the local parent image IDs, so a stale build is
# rebuilt after a Dockerfile change or a newer upstream parent pull.
IMAGE_LABEL = "io.nika.image"
BUILD_HASH_LABEL = "io.nika.build-hash"
_BUILD_HASH_VERSION = "2"


def nika_image(name: str) -> str:
    """Return the ``nika/<name>`` image reference for the current benchmark."""
    return f"{NIKA_IMAGE_PREFIX}{name}:{NIKA_IMAGE_TAG}"


# Scenario-local images required at deploy time. Upstream Kathara images
# (kathara/base, kathara/frr, …) are pulled, not listed here.
NIKA_IMAGE_DOCKERFILES: dict[str, str] = {
    nika_image("frr"): "Dockerfile.frr",
    nika_image("base"): "Dockerfile.base",
    nika_image("nginx"): "Dockerfile.nginx",
    nika_image("wireguard"): "Dockerfile.wireguard",
    nika_image("pox"): "Dockerfile.pox",
    nika_image("onos"): "Dockerfile.onos",
    nika_image("fabric-controller"): "Dockerfile.fabric-controller",
    nika_image("tc-bpf"): "Dockerfile.tc-bpf",
    nika_image("skopeo"): "Dockerfile.skopeo",
    nika_image("routinator"): "../isp/rpki/Dockerfile.routinator",
}

# Upstream images NIKA labs deploy directly, pinned to the last verified
# multi-arch index digest. Dockerfile FROM lines pin build parents the same way.
KATHARA_BASE_IMAGE = "kathara/base:latest@sha256:8a2e70ac8f51bb283b8549fd686f65f9a1ef4487fe2c81849cb74d301e61970b"
KATHARA_FRR_IMAGE = "kathara/frr:latest@sha256:3b7a3f630d8ecd9efddd4586ba2f0a41e2c462f13bc81dee8e3c6ebac183f57c"
KATHARA_P4_IMAGE = "kathara/p4:latest@sha256:62d4511908530028e275420b644d05e45ed9b4f5d786b56a63b5fb287dcce6ef"
KATHARA_SDN_IMAGE = "kathara/sdn:latest@sha256:19cb5b383367e1d204e76f7e6e65753fbc947647747879f24883558a5c3fd8c5"

# Images whose upstream base (or binaries) are single-arch. Builds and pulls
# must target this platform so arm64 hosts do not produce mixed-arch layers.
# nika/onos is intentionally absent: it is built for the host arch (Kathara
# base + host-arch JRE + ONOS Java tree copied from the amd64 upstream image).
NIKA_IMAGE_PLATFORMS: dict[str, str] = {}

# Dockerfiles that COPY --from a foreign-arch stage need BuildKit.
NIKA_IMAGE_BUILDKIT: frozenset[str] = frozenset({nika_image("onos")})

# Old tags from before the nika/* rename. Like other-version nika/* tags,
# ensure retags these when they match the current build, else removes them.
LEGACY_NIKA_IMAGE_NAMES: dict[str, tuple[str, ...]] = {
    nika_image("base"): ("kathara/nika-base",),
    nika_image("frr"): ("kathara/nika-frr",),
    nika_image("nginx"): ("kathara/nika-nginx",),
    nika_image("wireguard"): ("kathara/nika-wireguard",),
    nika_image("pox"): ("kathara/nika-pox",),
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
        suffix = image_repository(image).removeprefix(NIKA_IMAGE_PREFIX)
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


def image_repository(ref: str) -> str:
    """Return ``ref`` without its tag and digest."""
    name = ref.split("@", 1)[0]
    if ":" in name.rsplit("/", 1)[-1]:
        name = name.rsplit(":", 1)[0]
    return name


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


def _previous_builds(image: str) -> list[str]:
    """Local tags of ``image`` from other NIKA versions or pre-rename names."""
    repo = image_repository(image)
    tags = {
        tag
        for found in _get_client().images.list(name=repo)
        for tag in found.tags
        if tag != image and image_repository(tag) == repo
    }
    tags.update(
        name for name in LEGACY_NIKA_IMAGE_NAMES.get(image, ()) if image_exists(name)
    )
    return sorted(tags)


def _adopt_previous_build(image: str, current_hashes: set[str]) -> bool:
    """Retag a previous build identical to the current one, else remove them all.

    Returns True when ``image`` was recovered from a previous tag.
    """
    previous = _previous_builds(image)
    if not previous:
        return False
    for old in previous:
        if _image_build_hash(old) in current_hashes:
            retag_image(old, image)
            return True
    for old in previous:
        print(f"Removing outdated Docker image {old}...")
        try:
            _get_client().images.remove(old)
        except APIError as exc:
            print(f"Keeping {old}: {exc.explanation or exc}")
    return False


def _dockerfile_instructions(text: str) -> list[tuple[str, str]]:
    """Return ``(INSTRUCTION, arguments)`` pairs with continuations joined."""
    instructions: list[tuple[str, str]] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        pending += line
        keyword, _, rest = pending.partition(" ")
        instructions.append((keyword.upper(), rest.strip()))
        pending = ""
    return instructions


def _from_parents(text: str, build_args: dict[str, str]) -> list[str]:
    args = dict(build_args)
    stages: set[str] = set()
    parents: list[str] = []
    for keyword, rest in _dockerfile_instructions(text):
        if keyword == "ARG" and "=" in rest:
            name, _, default = rest.partition("=")
            args.setdefault(name.strip(), default.strip())
        elif keyword == "FROM":
            tokens = [token for token in rest.split() if not token.startswith("--")]
            ref = re.sub(
                r"\$\{?(\w+)\}?",
                lambda match: args.get(match.group(1), match.group(0)),
                tokens[0],
            )
            if ref not in stages and ref != "scratch" and ref not in parents:
                parents.append(ref)
            if len(tokens) >= 3 and tokens[1].lower() == "as":
                stages.add(tokens[2])
    return parents


def dockerfile_parents(image: str) -> list[str]:
    """Return external ``FROM`` references of a buildable image (no stage names)."""
    text = _dockerfile_for_image(image).read_text(encoding="utf-8")
    return _from_parents(text, _BUILD_ARGS)


def _copy_sources(dockerfile: Path) -> list[Path]:
    files: list[Path] = []
    for keyword, rest in _dockerfile_instructions(
        dockerfile.read_text(encoding="utf-8")
    ):
        if keyword not in ("COPY", "ADD"):
            continue
        tokens = rest.split()
        if any(token.startswith("--from") for token in tokens):
            continue
        sources = [token for token in tokens if not token.startswith("--")][:-1]
        for source in sources:
            for match in sorted(dockerfile.parent.glob(source)):
                if match.is_dir():
                    files.extend(sorted(p for p in match.rglob("*") if p.is_file()))
                elif match.is_file():
                    files.append(match)
    return files


def _local_image_id(image: str) -> str | None:
    try:
        return _get_client().images.get(image).id
    except ImageNotFound:
        return None


def _image_build_hash(image: str) -> str | None:
    """Return the build-hash label of a local image, or None if absent."""
    try:
        labels = _get_client().images.get(image).labels or {}
    except ImageNotFound:
        return None
    return labels.get(BUILD_HASH_LABEL)


def _hash_build(
    version: str, image: str, text: str, parents: list[tuple[str, str]]
) -> str:
    """Hash a build from its Dockerfile text, COPY sources, and parents.

    ``parents`` pairs the name hashed for each parent with the local
    reference whose image ID is hashed (skipped for digest-pinned parents).
    """
    dockerfile = _dockerfile_for_image(image)
    digest = hashlib.sha256()
    digest.update(f"{version}\0{_platform_for_image(image) or ''}\0".encode())
    digest.update(text.encode())
    for path in _copy_sources(dockerfile):
        digest.update(f"\0{path.relative_to(dockerfile.parent)}\0".encode())
        digest.update(path.read_bytes())
    for name, local_ref in parents:
        digest.update(f"\0{name}\0".encode())
        if "@sha256:" not in name:
            digest.update((_local_image_id(local_ref) or "missing").encode())
    return digest.hexdigest()


def build_hash(image: str) -> str:
    """Hash of everything that determines a local nika/* build.

    Local nika/* parents are hashed by repository and image ID, not tag, so
    an unchanged build keeps its hash across benchmark versions.
    """
    text = _dockerfile_for_image(image).read_text(encoding="utf-8")
    parents = [
        (image_repository(parent) if _is_locally_buildable(parent) else parent, parent)
        for parent in dockerfile_parents(image)
    ]
    return _hash_build(_BUILD_HASH_VERSION, image, text, parents)


def _unversioned_build_hash(image: str) -> str:
    """Hash the pre-versioning NIKA release gave the ``:latest`` build of ``image``.

    Those Dockerfiles named parents without digests (``kathara/base:latest``)
    and without the nika tag (``nika/base``). Undoing both edits and hashing
    the current parents' image IDs matches the old label only when the old
    build used the same Dockerfile, sources, and parent content.
    """
    text = _dockerfile_for_image(image).read_text(encoding="utf-8")
    text = text.replace(f"ARG {NIKA_IMAGE_TAG_ARG}\n", "")
    text = text.replace(f":${{{NIKA_IMAGE_TAG_ARG}}}", "")
    text = re.sub(r"^(FROM\s.*?)@sha256:[0-9a-f]{64}", r"\1", text, flags=re.M)
    old_parents = _from_parents(text, {})
    current = dockerfile_parents(image)
    if len(old_parents) != len(current):
        return ""
    return _hash_build("1", image, text, list(zip(old_parents, current)))


def build_nika_image(image: str, *, expected_hash: str | None = None) -> None:
    dockerfile = _dockerfile_for_image(image)
    docker_platform = _platform_for_image(image)
    labels = {
        IMAGE_LABEL: image,
        BUILD_HASH_LABEL: expected_hash or build_hash(image),
    }
    if docker_platform:
        _require_platform_support(image, docker_platform)
        print(
            f"Building Docker image {image} from {dockerfile.name} "
            f"(platform={docker_platform})..."
        )
    else:
        print(f"Building Docker image {image} from {dockerfile.name}...")

    if image in NIKA_IMAGE_BUILDKIT:
        cmd = [
            "docker",
            "build",
            "-f",
            dockerfile.name,
            "-t",
            image,
            "--network=host",
            *(f"--build-arg={key}={value}" for key, value in _BUILD_ARGS.items()),
            *(f"--label={key}={value}" for key, value in labels.items()),
            ".",
        ]
        if docker_platform:
            cmd[2:2] = ["--platform", docker_platform]
        env = {**os.environ, "DOCKER_BUILDKIT": "1"}
        try:
            subprocess.run(
                cmd,
                cwd=str(dockerfile.parent),
                env=env,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(f"Failed to build Docker image {image}") from exc
    else:
        build_kwargs: dict = {
            "path": str(dockerfile.parent),
            "dockerfile": dockerfile.name,
            "tag": image,
            "network_mode": "host",
            "rm": True,
            "buildargs": _BUILD_ARGS,
            "labels": labels,
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


def _ensure_built(image: str, *, force_rebuild: bool, ensured: set[str]) -> None:
    if image in ensured:
        return
    ensured.add(image)
    for parent in dockerfile_parents(image):
        if _is_locally_buildable(parent):
            _ensure_built(parent, force_rebuild=force_rebuild, ensured=ensured)
        elif "@sha256:" not in parent and not image_exists(parent):
            pull_image(parent)

    expected = build_hash(image)
    # A build retagged from before versioning keeps its old-format label.
    current_hashes = {expected, _unversioned_build_hash(image)}
    if not force_rebuild and not image_exists(image):
        _adopt_previous_build(image, current_hashes)
    if force_rebuild:
        reason = "force rebuild"
    elif not image_exists(image):
        reason = "missing"
    elif _image_build_hash(image) not in current_hashes:
        reason = "outdated"
    else:
        return
    print(f"Docker image {image}: {reason}")
    build_nika_image(image, expected_hash=expected)


def ensure_nika_docker_images(
    required_images: Iterable[str], *, force_rebuild: bool = False
) -> None:
    """Ensure required images are available locally and current.

    Locally buildable ``nika/*`` images are built when missing or when their
    build-hash label does not match the current Dockerfile, sources, and
    parent images. A missing image is first recovered by retagging an
    identical build from another NIKA version (e.g. ``nika/base:latest``) or
    a legacy ``kathara/nika-*`` name; non-matching old tags are removed before
    the rebuild. Parent images are ensured before their children.
    Other images (e.g. upstream ``kathara/p4``) are pulled when missing. With
    ``force_rebuild=True``, every buildable image is rebuilt.

    Images listed in ``NIKA_IMAGE_PLATFORMS`` are built/pulled for that
    platform. ``nika/onos`` builds for the host architecture.
    """
    required = {img for img in required_images if img}
    if not required:
        return

    buildable = {img for img in required if _is_locally_buildable(img)}
    pullable = required - buildable

    ensured: set[str] = set()
    for image in sorted(buildable):
        _ensure_built(image, force_rebuild=force_rebuild, ensured=ensured)

    to_pull = {img for img in pullable if not image_exists(img)}
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
