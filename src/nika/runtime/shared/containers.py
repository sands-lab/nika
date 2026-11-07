"""Docker container lifecycle helpers shared by lab runtime backends."""

from __future__ import annotations

import threading
from typing import Any

_client: Any = None
_client_lock = threading.Lock()


def docker_client() -> Any:
    """Return one process-wide Docker SDK client.

    ``docker.from_env()`` opens a new connection pool each call; per-exec or
    per-API construction leaks sockets on long benchmark runs. The client is
    safe to share across threads.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import docker

                _client = docker.from_env()
    return _client


def isolated_container_pid(container: Any) -> int:
    """Require a running container whose network namespace is not the host's."""
    from nika.runtime.base import RuntimeCapabilityError

    current = container
    seen = set()
    pid = None
    while current.id not in seen:
        seen.add(current.id)
        current.reload()
        current_pid = int((current.attrs.get("State") or {}).get("Pid") or 0)
        if current_pid <= 1:
            raise RuntimeCapabilityError(f"Container {current.id} is not running")
        if pid is None:
            pid = current_pid
        mode = (current.attrs.get("HostConfig") or {}).get("NetworkMode")
        if not isinstance(mode, str) or mode == "host":
            raise RuntimeCapabilityError(
                f"Refusing lab operation in host or unknown network namespace: {current.id}"
            )
        if not mode.startswith("container:"):
            return pid
        current = current.client.containers.get(mode.partition(":")[2])
    raise RuntimeCapabilityError("Cannot resolve container network namespace")


def pause_container(container: Any) -> None:
    container.reload()
    if container.status != "paused":
        container.pause()


def lift_cpu_cap(container: Any) -> None:
    """Raise a container's NanoCPUs cap to every host CPU.

    Docker ignores ``NanoCPUs=0`` on update, so the cap cannot be cleared,
    only raised. docker-py 7.x ``Container.update()`` omits NanoCPUs.
    """
    if not int((container.attrs.get("HostConfig") or {}).get("NanoCpus") or 0):
        return
    api = container.client.api
    url = api._url("/containers/{0}/update", container.id)
    resp = api._post_json(url, data={"NanoCPUs": api.info()["NCPU"] * 10**9})
    api._raise_for_status(resp)


def unpause_container(container: Any) -> None:
    container.reload()
    if container.status == "paused":
        container.unpause()
    elif container.status in {"created", "exited"}:
        container.start()
