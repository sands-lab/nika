"""Upstream Kathara images release 0.1.0 deploys by their ``latest`` tag.

Lab code deploys ``kathara/p4``, ``kathara/sdn``, and ``kathara/base`` by tag,
and the ``kathara/nika-*`` Dockerfiles build on them. These digests are the
images ``latest`` named while 0.1.0 was published (they were created in
March 2026) and that later NIKA versions pinned. The engine does not retag
images on a shared Docker daemon: it pulls a missing image by digest and
refuses one whose ``latest`` tag names a different image.
"""

from __future__ import annotations

from nika.benchmark_engine.protocol import EngineError

UPSTREAM_IMAGES: dict[str, str] = {
    "kathara/base": "sha256:8a2e70ac8f51bb283b8549fd686f65f9a1ef4487fe2c81849cb74d301e61970b",
    "kathara/frr": "sha256:3b7a3f630d8ecd9efddd4586ba2f0a41e2c462f13bc81dee8e3c6ebac183f57c",
    "kathara/p4": "sha256:62d4511908530028e275420b644d05e45ed9b4f5d786b56a63b5fb287dcce6ef",
    "kathara/sdn": "sha256:19cb5b383367e1d204e76f7e6e65753fbc947647747879f24883558a5c3fd8c5",
    "kathara/pox": "sha256:703ef8decbbede3ba6adc91af39e5d2b1c5e398e5049edcd5933bd4497267f69",
}


def ensure_upstream_images() -> None:
    """Make each ``kathara/<name>:latest`` the pinned image, or refuse to run."""
    import docker
    from docker.errors import ImageNotFound

    client = docker.from_env()
    for repo, digest in UPSTREAM_IMAGES.items():
        pinned = f"{repo}@{digest}"
        try:
            image = client.images.get(f"{repo}:latest")
        except ImageNotFound:
            client.images.pull(repo, tag=digest)
            client.images.get(pinned).tag(repo, "latest")
            continue
        if pinned not in (image.attrs.get("RepoDigests") or []):
            raise EngineError(
                f"{repo}:latest on this Docker host is not the image release 0.1.0 "
                f"deploys ({pinned}). Pull that digest and tag it {repo}:latest."
            )
