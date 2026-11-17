"""Versions of the host that runs a benchmark engine's environments."""

from __future__ import annotations

import platform

from nika.benchmark_engine.protocol import HostInfo


def local_host_info() -> HostInfo:
    """Docker server and kernel versions of this machine.

    The Docker version is ``None`` when the daemon cannot be reached.
    """
    try:
        import docker

        version = docker.from_env().version().get("Version")
        docker_version = str(version or "") or None
    except Exception:  # noqa: BLE001 - provenance must not stop a run
        docker_version = None
    return HostInfo(docker=docker_version, kernel=platform.release() or None)
