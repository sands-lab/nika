"""Resolve Docker containers for Kathara lab machines."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from docker.models.containers import Container
from Kathara.manager.Kathara import Kathara


def get_machine_container(*, lab_name: str, host_name: str) -> Container:
    """Return the Docker container for ``host_name`` inside ``lab_name``."""
    stats = next(
        Kathara.get_instance().get_machine_stats(
            machine_name=host_name, lab_name=lab_name
        ),
        None,
    )
    if stats is None:
        raise ValueError(
            f"No container found for host {host_name!r} in lab {lab_name!r}."
        )
    return stats.machine_api_object


def list_lab_containers(*, lab_name: str) -> list[dict[str, Any]]:
    """Return running Kathara devices for ``lab_name`` (docker-ps-like metadata)."""
    containers = Kathara.get_instance().get_machines_api_objects(lab_name=lab_name)
    rows: list[dict[str, Any]] = []
    for container in containers:
        labels = container.labels or {}
        image = (
            container.image.tags[0]
            if container.image.tags
            else container.image.short_id
        )
        rows.append(
            {
                "container_id": container.short_id,
                "name": labels.get("name", "—"),
                "container_name": container.name.lstrip("/"),
                "image": image,
                "status": container.status,
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def link_members(link: Any) -> tuple[str, ...]:
    """Machine names attached to one Kathara collision domain, in Docker order."""
    return tuple(
        name
        for name in (
            (container.labels or {}).get("name") for container in link.containers
        )
        if name
    )


def link_neighbors(links: Iterable[Any], node: str) -> list[str]:
    """Machines sharing a collision domain with ``node``.

    A collision domain may hold one endpoint (a dangling link) or several
    (a shared LAN); every other member is a neighbor.
    """
    results: list[str] = []
    for link in links:
        if not link.name:
            continue
        members = link_members(link)
        if node in members:
            results.extend(name for name in members if name != node)
    return results
