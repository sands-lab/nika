"""Build and lightly probe locally buildable nika/* images on the host arch."""

from __future__ import annotations

import os

import docker
import pytest

from nika.net_env.utils.kathara.docker_files.docker_images import (
    build_nika_image,
    host_machine_arch,
    image_architecture,
    image_exists,
)
from tests.ci.constants import CI_NIKA_IMAGES
from tests.support.prerequisites import docker_available

pytestmark = [
    pytest.mark.ci_smoke,
    pytest.mark.skipif(not docker_available(), reason="Docker not available"),
]

# Images whose Dockerfiles FROM another locally built nika/* tag.
_NIKA_IMAGE_DEPS: dict[str, tuple[str, ...]] = {
    "nika/fabric-controller": ("nika/base",),
}


def _selected_images() -> list[str]:
    selected = os.environ.get("NIKA_CI_IMAGE", "").strip()
    if selected:
        if selected not in CI_NIKA_IMAGES:
            raise ValueError(
                f"Unknown NIKA_CI_IMAGE={selected!r}; expected one of {CI_NIKA_IMAGES}"
            )
        return [selected]
    return list(CI_NIKA_IMAGES)


def _ensure_image(image: str) -> None:
    for dep in _NIKA_IMAGE_DEPS.get(image, ()):
        if not image_exists(dep):
            build_nika_image(dep)
    build_nika_image(image)


@pytest.mark.parametrize("image", _selected_images(), ids=lambda i: i.replace("/", "_").replace(":", "_"))
def test_nika_image_build_and_arch(image: str) -> None:
    """Build (or rebuild) one nika image and assert host-arch + creatable."""
    _ensure_image(image)
    assert image_exists(image), f"{image} missing after build"

    expected = host_machine_arch()
    actual = image_architecture(image)
    assert actual == expected, f"{image} Architecture={actual!r}, expected {expected!r}"

    client = docker.from_env()
    container = client.containers.create(image, command=["true"])
    try:
        assert container.id
    finally:
        container.remove(force=True)
