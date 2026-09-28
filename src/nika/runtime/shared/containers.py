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


def pause_container(container: Any) -> None:
    container.reload()
    if container.status != "paused":
        container.pause()


def unpause_container(container: Any) -> None:
    container.reload()
    if container.status == "paused":
        container.unpause()
    elif container.status in {"created", "exited"}:
        container.start()
