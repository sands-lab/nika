"""Helpers for Docker/cgroup CPU quota injection and recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nika.runtime.base import LabRuntime

ORIGINAL_NANO_CPUS_PATH = "/tmp/nika_cpu_quota_original"
NANOCPUS_PER_CPU = 1_000_000_000
DEFAULT_CPU_PERIOD_US = 100_000


def cpu_quota_to_nano_cpus(cpu_quota: float) -> int:
    """Convert a fractional CPU quota (e.g. 0.25) to Docker NanoCpus."""
    if cpu_quota <= 0:
        raise ValueError(f"cpu_quota must be positive, got {cpu_quota}")
    return int(round(cpu_quota * NANOCPUS_PER_CPU))


def nano_cpus_to_cfs(nano_cpus: int) -> tuple[int, int]:
    """Return (cpu_period, cpu_quota) for CFS when NanoCPUs is unavailable.

    ``nano_cpus == 0`` means unlimited (cpu_quota=-1).
    """
    if nano_cpus < 0:
        raise ValueError(f"nano_cpus must be >= 0, got {nano_cpus}")
    if nano_cpus == 0:
        return DEFAULT_CPU_PERIOD_US, -1
    quota = max(1000, int(round(nano_cpus * DEFAULT_CPU_PERIOD_US / NANOCPUS_PER_CPU)))
    return DEFAULT_CPU_PERIOD_US, quota


def read_nano_cpus(runtime: "LabRuntime", host: str) -> int:
    """Return the container's effective NanoCpus (0 means unlimited)."""
    container = runtime.get_container(host)
    container.reload()
    host_config = container.attrs.get("HostConfig") or {}
    nano = host_config.get("NanoCpus")
    if nano is not None and int(nano) != 0:
        return int(nano)
    period = int(host_config.get("CpuPeriod") or 0)
    quota = int(host_config.get("CpuQuota") or 0)
    if period > 0 and quota > 0:
        return int(round(quota / period * NANOCPUS_PER_CPU))
    return 0


def _update_container_resources(container: Any, data: dict[str, Any]) -> None:
    """POST /containers/{id}/update with a raw resource body.

    docker-py 7.x ``Container.update()`` omits NanoCPUs, but Kathara creates
    containers with NanoCPUs set, and Docker rejects CpuPeriod/CpuQuota updates
    in that state. The Engine API still accepts NanoCPUs directly.
    """
    api = container.client.api
    url = api._url("/containers/{0}/update", container.id)
    resp = api._post_json(url, data=data)
    api._raise_for_status(resp)


def set_nano_cpus(runtime: "LabRuntime", host: str, nano_cpus: int) -> None:
    """Apply a Docker NanoCpus limit (0 restores unlimited)."""
    if nano_cpus < 0:
        raise ValueError(f"nano_cpus must be >= 0, got {nano_cpus}")
    container = runtime.get_container(host)
    container.reload()
    host_config = container.attrs.get("HostConfig") or {}
    has_nano = int(host_config.get("NanoCpus") or 0) != 0

    if nano_cpus == 0:
        # Cannot mix NanoCPUs with CpuPeriod/CpuQuota in one update. Clear
        # NanoCPUs alone; Docker treats 0 as unlimited for that field.
        if has_nano:
            _update_container_resources(container, {"NanoCPUs": 0})
            container.reload()
            host_config = container.attrs.get("HostConfig") or {}
            has_nano = int(host_config.get("NanoCpus") or 0) != 0
        if not has_nano:
            period, quota = nano_cpus_to_cfs(0)
            try:
                container.update(cpu_period=period, cpu_quota=quota)
            except Exception:
                # Some runtimes already have no CFS quota; ignore.
                pass
        return

    if has_nano or nano_cpus > 0:
        # Prefer NanoCPUs whenever the container already uses it, or when
        # applying a positive limit (matches Kathara create-time caps).
        _update_container_resources(container, {"NanoCPUs": int(nano_cpus)})
        return

    period, quota = nano_cpus_to_cfs(nano_cpus)
    container.update(cpu_period=period, cpu_quota=quota)


def persist_original_nano_cpus(
    runtime: "LabRuntime", host: str, nano_cpus: int
) -> None:
    runtime.write_file(host, ORIGINAL_NANO_CPUS_PATH, f"{int(nano_cpus)}\n")


def load_original_nano_cpus(runtime: "LabRuntime", host: str) -> int | None:
    raw = runtime.exec(
        host,
        f"cat {ORIGINAL_NANO_CPUS_PATH} 2>/dev/null || true",
        timeout=10,
    ).strip()
    if not raw:
        return None
    try:
        return int(raw.splitlines()[0].strip())
    except ValueError:
        return None


def clear_original_nano_cpus(runtime: "LabRuntime", host: str) -> None:
    runtime.exec(
        host,
        f"rm -f {ORIGINAL_NANO_CPUS_PATH} 2>/dev/null || true",
        timeout=5,
    )
