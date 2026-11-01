"""Versions of the host that runs a benchmark engine's environments."""

from __future__ import annotations

import platform

from nika.benchmark_engine.protocol import HostInfo


def local_host_info() -> HostInfo:
    """Docker server and kernel versions of this machine.

    The Docker version is ``None`` when the daemon cannot be reached.
    """
    from nika.runtime.shared.containers import docker_client

    try:
        docker = str(docker_client().version().get("Version") or "") or None
    except Exception:  # noqa: BLE001 - provenance must not stop a run
        docker = None
    return HostInfo(docker=docker, kernel=platform.release() or None)


def engine_host_info() -> HostInfo:
    """Host of the environments this NIKA deploys: NIKA Remote's, or this machine's."""
    from nika.remote.config import is_remote_enabled, load_remote_config

    if not is_remote_enabled():
        return local_host_info()
    from nika.remote.client import RemoteClient

    try:
        health = RemoteClient(load_remote_config()).health()
    except Exception:  # noqa: BLE001 - provenance must not stop a run
        return HostInfo()
    return HostInfo(docker=health.docker, kernel=health.kernel)
