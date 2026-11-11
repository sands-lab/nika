"""Admission checks for labs whose routers run native vendor images."""

from __future__ import annotations

from pathlib import Path

GIB = 1024**3


def native_lab_memory(*, routers: int, per_router: int, hosts: int) -> int:
    """Estimated bytes a lab with native vendor routers needs, including headroom.

    The estimate is a conservative admission bound, not a limit on vendor
    process usage.
    """
    return 2 * GIB + routers * per_router + hosts * (GIB // 4)


def require_native_lab_memory(
    label: str, *, routers: int, per_router: int, hosts: int
) -> None:
    """Reject a deployment whose :func:`native_lab_memory` exceeds what is available.

    It runs before any container is created.
    """
    available = next(
        int(line.split()[1]) * 1024
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith("MemAvailable:")
    )
    required = native_lab_memory(routers=routers, per_router=per_router, hosts=hosts)
    if required > available:
        raise RuntimeError(
            f"{label} with {routers} routers needs an estimated "
            f"{required / GIB:.1f} GiB including headroom; only "
            f"{available / GIB:.1f} GiB is available. Use a smaller topology "
            "or a host with more memory."
        )
