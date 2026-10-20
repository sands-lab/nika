"""Shared prerequisites for real scenario synthesis and verification."""

import pytest

from tests.support.prerequisites import docker_available


@pytest.fixture
def netcomplete_image():
    if not docker_available():
        pytest.skip("Docker not available")
    import docker
    from docker.errors import ImageNotFound

    client = docker.from_env()
    try:
        client.images.get("nika/netcomplete:experiment")
    except ImageNotFound:
        pytest.skip("Build the pinned NetComplete experiment image first")
    finally:
        client.close()
    return "nika/netcomplete:experiment"


@pytest.fixture
def katch_image():
    if not docker_available():
        pytest.skip("Docker not available")
    import docker
    from docker.errors import ImageNotFound
    from nika.validation.netkat.verifier import KATCH_IMAGE

    client = docker.from_env()
    try:
        client.images.get(KATCH_IMAGE)
    except ImageNotFound:
        pytest.skip("Install the pinned KATch experiment image first")
    finally:
        client.close()
    return KATCH_IMAGE
