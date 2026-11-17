"""The container images release 0.1.0's labs run, checked before a run.

Lab code deploys ``kathara/p4``, ``kathara/sdn``, ``kathara/base``, and
``kathara/frr`` by their ``latest`` tag, and the ``kathara/nika-*`` images
that most labs deploy are built on them (``kathara/nika-pox`` on
``kathara/pox``). These digests are the images ``latest`` named while 0.1.0
was published (they were created in March 2026) and that later NIKA versions
pinned.

Other NIKA versions on a shared Docker daemon use the same tags, sometimes
for images built from other Dockerfiles. The engine never retags or removes
an image there: it pulls a missing upstream image by digest, leaves a missing
``kathara/nika-*`` image for the deploy to build, and refuses a run when a
tag names a different image.
"""

from __future__ import annotations

import re

from nika.benchmark_engine.protocol import EngineError
from nika.net_env.kathara.utils.docker_files.docker_images import (
    DOCKER_FILES_DIR,
    NIKA_IMAGE_DOCKERFILES,
)

# Upstream images labs deploy by their ``latest`` tag.
UPSTREAM_IMAGES: dict[str, str] = {
    "kathara/base": "sha256:8a2e70ac8f51bb283b8549fd686f65f9a1ef4487fe2c81849cb74d301e61970b",
    "kathara/frr": "sha256:3b7a3f630d8ecd9efddd4586ba2f0a41e2c462f13bc81dee8e3c6ebac183f57c",
    "kathara/p4": "sha256:62d4511908530028e275420b644d05e45ed9b4f5d786b56a63b5fb287dcce6ef",
    "kathara/sdn": "sha256:19cb5b383367e1d204e76f7e6e65753fbc947647747879f24883558a5c3fd8c5",
}
# Locally built images release 0.1.0's labs deploy.
NIKA_IMAGES = (
    "kathara/nika-base",
    "kathara/nika-frr",
    "kathara/nika-nginx",
    "kathara/nika-pox",
    "kathara/nika-wireguard",
)

_FROM_RE = re.compile(r"^FROM\s+(\S+)@(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)


def _rebuild_hint(image: str) -> str:
    return (
        f"Remove the tag (docker image rm {image}) so the next deploy builds "
        "it from release 0.1.0's Dockerfile."
    )


def _normalize(command: str) -> str:
    """A build step's shell command, as the Dockerfile or image history shows it."""
    command = command.replace("\\\n", " ")
    command = re.sub(r"^\s*(RUN\s+)?(\|\d+\s+\S+\s+)?(/bin/sh -c\s+)?", "", command)
    command = command.split("# buildkit")[0]
    return " ".join(command.split())


def _dockerfile(image: str) -> tuple[str, str, str]:
    """``(parent repo, parent digest, RUN command)`` of a ``kathara/nika-*`` image."""
    text = (DOCKER_FILES_DIR / NIKA_IMAGE_DOCKERFILES[image]).read_text()
    match = _FROM_RE.search(text)
    if match is None:
        raise EngineError(f"The Dockerfile of {image} does not pin its base image.")
    parent = match.group(1).split(":")[0]
    run = text[match.end() :].strip()
    return parent, match.group(2), _normalize(run)


def _layers(image) -> list[str]:
    return list((image.attrs.get("RootFS") or {}).get("Layers") or [])


def ensure_lab_images() -> None:
    """Refuse a run when an image tag names an image 0.1.0 did not deploy."""
    import docker
    from docker.errors import ImageNotFound

    client = docker.from_env()

    def pinned(repo: str, digest: str):
        reference = f"{repo}@{digest}"
        try:
            return client.images.get(reference)
        except ImageNotFound:
            client.images.pull(repo, tag=digest)
            return client.images.get(reference)

    for repo, digest in UPSTREAM_IMAGES.items():
        reference = f"{repo}@{digest}"
        try:
            image = client.images.get(f"{repo}:latest")
        except ImageNotFound:
            pinned(repo, digest).tag(repo, "latest")
            continue
        if reference not in (image.attrs.get("RepoDigests") or []):
            raise EngineError(
                f"{repo}:latest on this Docker host is not the image release 0.1.0 "
                f"deploys ({reference}). Pull that digest and tag it {repo}:latest."
            )

    for name in NIKA_IMAGES:
        try:
            image = client.images.get(name)
        except ImageNotFound:
            continue
        parent_repo, parent_digest, run = _dockerfile(name)
        parent_layers = _layers(pinned(parent_repo, parent_digest))
        layers = _layers(image)
        top = (image.history() or [{}])[0].get("CreatedBy") or ""
        if (
            layers[: len(parent_layers)] != parent_layers
            or len(layers) != len(parent_layers) + 1
            or _normalize(top) != run
        ):
            raise EngineError(
                f"{name} on this Docker host was not built from release 0.1.0's "
                f"Dockerfile on {parent_repo}@{parent_digest}. {_rebuild_hint(name)}"
            )
