"""Business traffic of the dc_clos fabric (always on, every case).

Each external client stands for the users behind it. They resolve the pod
web names (``web<i>.pod<p>``) through the pod DNS servers and load pages and
small objects from every pod's web servers every few seconds, and every few
minutes download a large object (``large.bin``, 2 MB) from one of them.
"""

from __future__ import annotations


from nika.net_env.utils.rack_workload import seed_for
from nika.traffic.workload import (
    BULK_DOWNLOAD,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
    routine_change_processes,
)


def build_workload(
    clients: list[str], web_urls: list[str], routers: list[str]
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
    processes += routine_change_processes(routers)
    return BusinessWorkload(
        processes=tuple(processes),
        exercises=tuple(WorkloadExercise(BULK_DOWNLOAD, (c,)) for c in clients),
    )
