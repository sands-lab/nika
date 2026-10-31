"""Docker container lifecycle helpers shared by lab runtime backends."""

from __future__ import annotations

import threading
from typing import Any

_client: Any = None
_client_lock = threading.Lock()

# Privileged netns helpers must not sit on the shared host if netlink blocks.
_HELPER_WAIT_SEC = 60.0
_HELPER_STOP_SEC = 10


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


def remove_lab_helpers(lab_name: str) -> None:
    """Remove controller-side helpers labelled ``nika.lab_name=<lab_name>``."""
    client = docker_client()
    label = f"nika.lab_name={lab_name}"
    for container in client.containers.list(all=True, filters={"label": label}):
        container.remove(force=True)


def run_labelled_helper(
    client: Any,
    image: str,
    command: Any,
    *,
    lab_name: str,
    timeout_sec: float = _HELPER_WAIT_SEC,
    stop_timeout_sec: int = _HELPER_STOP_SEC,
    **kwargs: Any,
) -> bytes:
    """Run a short helper labelled for ``lab_name`` and always remove it.

    The helper is created detached so a blocked netlink call can be stopped.
    ``remove_lab_helpers`` can still find it if this process dies first.
    """
    labels = dict(kwargs.pop("labels", None) or {})
    labels["nika.lab_name"] = lab_name
    kwargs.pop("remove", None)
    kwargs.pop("detach", None)
    container = client.containers.run(
        image,
        command=command,
        detach=True,
        labels=labels,
        **kwargs,
    )
    try:
        try:
            result = container.wait(timeout=int(timeout_sec))
        except Exception as exc:
            try:
                container.stop(timeout=stop_timeout_sec)
            except Exception:
                pass
            raise RuntimeError(
                f"lab helper did not finish within {timeout_sec:.0f}s"
            ) from exc
        output = container.logs() or b""
        if isinstance(output, str):
            output = output.encode()
        code = int((result or {}).get("StatusCode", 1))
        if code:
            detail = output.decode("utf-8", "replace").strip() or f"exit {code}"
            raise RuntimeError(f"lab helper exited {code}: {detail}")
        return output
    finally:
        try:
            container.remove(force=True)
        except Exception:
            pass


def unpause_container(container: Any) -> None:
    container.reload()
    if container.status == "paused":
        container.unpause()
    elif container.status in {"created", "exited"}:
        container.start()
