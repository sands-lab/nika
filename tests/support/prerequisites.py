from __future__ import annotations

import os
import shutil
from typing import Any


def _docker_mod() -> Any | None:
    try:
        import docker
    except ImportError:
        return None
    return docker


def docker_available() -> bool:
    docker = _docker_mod()
    if docker is None:
        return False
    try:
        docker.from_env().ping()
    except Exception:
        return False
    return True


def commands_available(*commands: str) -> bool:
    return all(shutil.which(command) for command in commands)


def containerlab_prerequisites() -> bool:
    return docker_available() and commands_available("clab", "gnmic")


# Historical alias used by Containerlab API and pipeline tests.
min3clos_prerequisites = containerlab_prerequisites


def docker_image_available(image: str) -> bool:
    if not docker_available():
        return False
    docker = _docker_mod()
    assert docker is not None
    try:
        return bool(docker.from_env().images.list(name=image))
    except Exception:
        return False


def privileged_lab_supported() -> bool:
    """k3s labs need Docker privileged containers.

    Host root is not required when the user can talk to the Docker engine
    (typically via the ``docker`` group); NIKA patches Kathara's root-only gate.
    """
    if os.geteuid() == 0:
        return True
    return docker_available()


def linux_vrf_available() -> bool:
    """Return True when the host kernel can create Linux VRF devices.

    Kathara/enterprise_branch VRFs use the host kernel. GitHub-hosted Azure
    kernels often omit ``vrf.ko``; CI sets ``NIKA_CI_VRF=0|1`` after a probe.
    """
    forced = os.environ.get("NIKA_CI_VRF", "").strip()
    if forced == "0":
        return False
    if forced == "1":
        return True

    import subprocess

    name = "nika_vrf_probe"
    create = subprocess.run(
        ["ip", "link", "add", name, "type", "vrf", "table", "110"],
        capture_output=True,
        text=True,
        check=False,
    )
    if create.returncode != 0:
        create = subprocess.run(
            ["sudo", "-n", "ip", "link", "add", name, "type", "vrf", "table", "110"],
            capture_output=True,
            text=True,
            check=False,
        )
    if create.returncode != 0:
        return False
    subprocess.run(["ip", "link", "del", name], capture_output=True, check=False)
    subprocess.run(
        ["sudo", "-n", "ip", "link", "del", name], capture_output=True, check=False
    )
    return True
