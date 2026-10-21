"""Shared prerequisites for real routing synthesis and verification."""

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
