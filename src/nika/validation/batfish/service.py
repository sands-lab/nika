from __future__ import annotations

import time
from typing import Any

PYBATFISH_VERSION = "2025.7.7.2423"
BATFISH_IMAGE = (
    "batfish/batfish:2025.07.07.2423@"
    "sha256:5515157739b7dca9a63b04e7a4a8f9c3d94ab0bdb8cd79ece57a5a5204307fbb"
)
BATFISH_CONTAINER_NAME = "nika-batfish-2025-07-07-2423"


def ensure_batfish_service(
    *, host: str = "127.0.0.1", port: int = 9996, timeout_sec: float = 120
) -> dict[str, Any]:
    """Start or reuse the pinned official Batfish container."""
    if host not in {"127.0.0.1", "localhost"}:
        return _wait_for_service(host, port, timeout_sec)
    try:
        import docker
        from docker.errors import NotFound
    except ImportError as exc:
        raise RuntimeError(
            "Batfish validation requires `uv sync --extra batfish`."
        ) from exc

    client = docker.from_env()
    try:
        container = client.containers.get(BATFISH_CONTAINER_NAME)
        if container.status != "running":
            container.start()
    except NotFound:
        client.images.pull(BATFISH_IMAGE)
        container = client.containers.run(
            BATFISH_IMAGE,
            name=BATFISH_CONTAINER_NAME,
            detach=True,
            ports={"9996/tcp": ("127.0.0.1", port)},
        )
    versions = _wait_for_service(host, port, timeout_sec)
    versions["container_id"] = container.id
    versions["container_image"] = BATFISH_IMAGE
    return versions


def _wait_for_service(host: str, port: int, timeout_sec: float) -> dict[str, Any]:
    try:
        from pybatfish.client.session import Session
    except ImportError as exc:
        raise RuntimeError(
            "Batfish validation requires `uv sync --extra batfish`."
        ) from exc
    deadline = time.monotonic() + timeout_sec
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            session = Session(host=host, port_v2=port, load_questions=False)
            return dict(session.get_component_versions())
        except Exception as exc:  # noqa: BLE001 - service is still starting
            last_error = exc
            time.sleep(1)
    raise RuntimeError(f"Batfish did not become ready at {host}:{port}: {last_error}")
