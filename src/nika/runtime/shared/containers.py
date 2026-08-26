"""Docker container lifecycle helpers shared by lab runtime backends."""

from __future__ import annotations

from typing import Any


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
