"""Shared prerequisites for real scenario synthesis and verification."""

import pytest

from tests.support.prerequisites import docker_available


@pytest.fixture(scope="session")
def batfish_verifier():
    """Share one pinned analyzer on an allocated port, preserving other services."""
    import docker
    from docker.errors import ImageNotFound
    from uuid import uuid4

    from nika.validation.batfish.service import BATFISH_IMAGE, _wait_for_service
    from nika.validation.batfish.verifier import BatfishVerifier

    if not docker_available():
        pytest.skip("Docker not available")
    client = docker.from_env()
    container = None
    try:
        try:
            client.images.get(BATFISH_IMAGE)
        except ImageNotFound:
            pytest.skip("Build the pinned Batfish experiment image first")
        container = client.containers.create(
            BATFISH_IMAGE,
            name=f"nika-test-batfish-{uuid4().hex[:12]}",
            ports={"9996/tcp": ("127.0.0.1", None)},
            environment={"JAVA_TOOL_OPTIONS": "-Xmx8g"},
            mem_limit="10g",
            nano_cpus=2_000_000_000,
        )
        container.start()
        container.reload()
        port = int(
            container.attrs["NetworkSettings"]["Ports"]["9996/tcp"][0]["HostPort"]
        )
        _wait_for_service("127.0.0.1", port, 120)
        yield BatfishVerifier(host="127.0.0.1", port=port)
    finally:
        if container is not None:
            container.remove(force=True)
        client.close()


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
