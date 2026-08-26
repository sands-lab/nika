from __future__ import annotations

import os
import shutil

import docker


def docker_available() -> bool:
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
