"""Business traffic between two servers on either side of a routed path.

min3clos, iosxr_simple_bgp and routeros_simple_bgp each connect two hosts
across a few routers. Their users run:

- **Replication.** Each host publishes a data set over HTTP (port 8080); the
  second host pulls the first's at 2 Mbit/s and the first pulls the second's
  at 1 Mbit/s, back to back. Under a router send cap both rates shrink by the
  same factor to fit the business share of it.
- **Sessions.** The second host runs a web/push service; the first keeps
  persistent sessions and page loads to it.

Both stay far below what the path forwards, so a healthy lab shows no loss or
queueing.
"""

from __future__ import annotations

from collections.abc import Iterable

from nika.traffic.workload import (
    BULK_DOWNLOAD,
    BULK_TRANSIT,
    BULK_TRANSIT_MIN_KBPS,
    BUSINESS_RATE_SHARE,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
    rate_budget_kbps,
)

BULK_PORT = 8080
# Host startup line that creates the published data set (sparse file).
DATA_SET_STARTUP = "mkdir -p /var/www && truncate -s 8M /var/www/large.bin"
REPLICATION_KBPS = (2000, 1000)


def build_pair_workload(
    first: tuple[str, str],
    second: tuple[str, str],
    first_to_second: Iterable[tuple[str, str]],
    rate_limit_kbps: int | None = None,
) -> BusinessWorkload:
    """Workload between ``first`` and ``second``, each ``(node, ipv4)``.

    ``first_to_second`` lists the egress ``(node, interface)`` hops from
    ``first`` toward ``second``; they carry ``bulk_transit`` when the
    replication that way reaches ``BULK_TRANSIT_MIN_KBPS``.
    ``rate_limit_kbps`` is the routers' per-interface send cap, if any.
    """
    budget = rate_budget_kbps(rate_limit_kbps, BUSINESS_RATE_SHARE)
    scale = min(1.0, budget / max(REPLICATION_KBPS)) if budget else 1.0
    forward, backward = (int(kbps * scale) for kbps in REPLICATION_KBPS)
    ips = dict((first, second))
    processes: list[WorkloadProcess] = []
    exercises: list[WorkloadExercise] = []
    for server, client, kbps in (
        (first[0], second[0], forward),
        (second[0], first[0], backward),
    ):
        processes.append(
            WorkloadProcess(
                node=server,
                app="file_server",
                command="filedist",
                args=("--root", "/var/www", "--port", str(BULK_PORT)),
            )
        )
        url = f"http://{ips[server]}:{BULK_PORT}/large.bin?rate={kbps}k"
        processes.append(
            WorkloadProcess(
                node=client,
                app="file_sync",
                command="filesync",
                args=(url, "--pause", "1-3", "--seed", str(kbps)),
            )
        )
        exercises.append(WorkloadExercise(BULK_DOWNLOAD, (client,)))
    if forward >= BULK_TRANSIT_MIN_KBPS:
        exercises += [WorkloadExercise(BULK_TRANSIT, hop) for hop in first_to_second]
    processes += [
        WorkloadProcess(
            node=second[0],
            app="web_service",
            command="portal",
            args=("--port", "80", "--seed", "2"),
        ),
        WorkloadProcess(
            node=first[0],
            app="web_client",
            command="webclient",
            args=(
                second[1],
                "--sessions", "4", "--lifetime", "120-600",
                "--burst", "3", "--burst-every", "20-40", "--burst-kb", "8-64",
                "--seed", "1",
            ),
        ),
    ]  # fmt: skip
    return BusinessWorkload(processes=tuple(processes), exercises=tuple(exercises))
