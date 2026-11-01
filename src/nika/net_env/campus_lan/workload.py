"""Business traffic of the campus_lan network (always on, every case).

- **Browsing.** Every PC user opens pages on the web farm (web0-web3.local)
  and the load-balanced web99.local every few tens of seconds, resolving the
  names through the campus DNS server.
- **Software catalog.** The teaching-lab PCs, every PC in the 10.2.<dist>.0/24
  access blocks behind router_core_2, refresh the software catalog from the
  campus repository every few minutes: ``https://web0.local/packages/
  catalog.txt`` with gzip content encoding, about 3.3 MB on the wire.

Under a router send cap the transfers are read at a limited rate
(``pace_browsers``).
"""

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

CATALOG_URL = "https://web0.local/packages/catalog.txt"
PAGE_URLS = tuple(
    f"http://{name}.local{path}"
    for name in ("web0", "web1", "web2", "web3", "web99")
    for path in ("/", "/small")
)
LAB_PC_PREFIX = "pc_2_"


def build_workload(
    hosts: list[str], routers: list[str], rate_limit_kbps: int | None = None
) -> BusinessWorkload:
    processes: list[WorkloadProcess] = []
    exercises: list[WorkloadExercise] = []
    for pc in sorted(h for h in hosts if h.startswith("pc_")):
        processes.append(
            WorkloadProcess(
                node=pc,
                app="browser",
                command="browser",
                args=(*PAGE_URLS, "--pause", "15-45", "--seed", seed_for(pc)),
            )
        )
        if pc.startswith(LAB_PC_PREFIX):
            processes.append(
                WorkloadProcess(
                    node=pc,
                    app="browser",
                    command="catalog-sync",
                    args=(
                        CATALOG_URL,
                        "--gzip",
                        "--pause",
                        "180-360",
                        "--timeout",
                        "20",
                        "--seed",
                        seed_for(pc),
                    ),
                )
            )
            exercises.append(WorkloadExercise(BULK_DOWNLOAD, (pc,)))
    processes = pace_browsers(processes, rate_limit_kbps)
    processes += routine_change_processes(routers)
    return BusinessWorkload(processes=tuple(processes), exercises=tuple(exercises))
