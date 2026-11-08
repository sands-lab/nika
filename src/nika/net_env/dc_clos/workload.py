"""Rack clients browse named fabric web services and download large objects."""

from __future__ import annotations


from nika.net_env.utils.rack_workload import seed_for
from nika.traffic.workload import (
    BULK_DOWNLOAD,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
    pace_browsers,
    routine_change_processes,
)


def build_workload(
    clients: list[str],
    web_urls: list[str],
    routers: list[str],
    rate_limit_kbps: int | None = None,
) -> BusinessWorkload:
    sites = [url.rstrip("/") for url in web_urls]
    pages = tuple(f"{site}{path}" for site in sites for path in ("/", "/small.bin"))
    large = tuple(f"{site}/large.bin" for site in sites)
    processes: list[WorkloadProcess] = []
    for client in clients:
        seed = seed_for(client)
        processes += [
            WorkloadProcess(
                node=client,
                app="browser",
                command="browser",
                args=(*pages, "--users", "2", "--pause", "3-10", "--seed", seed),
            ),
            WorkloadProcess(
                node=client,
                app="browser",
                command="downloader",
                args=(*large, "--pause", "120-300", "--seed", seed),
            ),
        ]
    processes = pace_browsers(processes, rate_limit_kbps)
    processes += routine_change_processes(routers)
    return BusinessWorkload(
        processes=tuple(processes),
        exercises=tuple(WorkloadExercise(BULK_DOWNLOAD, (c,)) for c in clients),
    )
