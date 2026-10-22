"""Business traffic shared by the leaf-spine rack fabrics.

sdn_l3_clos and p4_dc_fabric give every leaf one web server (``web_<leaf>``)
and client hosts. Their users load small objects and pages from the web
servers every few seconds and download a large object every few minutes.
p4_dc_fabric also runs a partition/aggregate query service: see
``fanout_processes``.
"""

from __future__ import annotations

import zlib
from collections.abc import Iterable

from nika.net_env.utils.download_object import download_object_commands
from nika.traffic.workload import BULK_DOWNLOAD, WorkloadExercise, WorkloadProcess

FANOUT_PORT = 11311


def seed_for(node: str) -> str:
    return str(zlib.crc32(node.encode()) & 0xFFFF)


def web_content_commands(large_mb: int) -> list[str]:
    """Startup commands that publish the objects users fetch from a web server."""
    return [
        "mkdir -p /var/www/html",
        "dd if=/dev/urandom of=/var/www/html/small.bin bs=1024 count=16 status=none",
        f"dd if=/dev/urandom of=/var/www/html/large.bin bs=1M count={large_mb} "
        "status=none",
        *download_object_commands("/var/www/html"),
    ]


def client_processes(
    clients: Iterable[str],
    web_ips: Iterable[str],
    *,
    page_pause: str,
    large_pause: str,
) -> list[WorkloadProcess]:
    sites = [f"http://{ip}" for ip in web_ips]
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
                args=(*pages, "--pause", page_pause, "--seed", seed),
            ),
            WorkloadProcess(
                node=client,
                app="browser",
                command="downloader",
                args=(*large, "--pause", large_pause, "--seed", seed),
            ),
        ]
    return processes


def bulk_downloads(clients: Iterable[str]) -> list[WorkloadExercise]:
    """Every client's downloader repeatedly pulls large.bin into it."""
    return [WorkloadExercise(BULK_DOWNLOAD, (client,)) for client in clients]


def fanout_processes(
    aggregator: str,
    workers: dict[str, str],
    *,
    count: int,
    size: int,
    period: str,
    worker_command: str = "shard",
    aggregator_command: str = "query-frontend",
) -> list[WorkloadProcess]:
    """A query front end on ``aggregator`` and one shard per ``workers`` node.

    ``workers`` maps node name to IPv4 address.
    """
    processes = [
        WorkloadProcess(
            node=node,
            app="fanout",
            command=worker_command,
            args=("worker", "--port", str(FANOUT_PORT)),
        )
        for node in workers
    ]
    targets: list[str] = []
    for ip in workers.values():
        targets += ["--worker", f"{ip}:{FANOUT_PORT}"]
    processes.append(
        WorkloadProcess(
            node=aggregator,
            app="fanout",
            command=aggregator_command,
            args=(
                "aggregator",
                *targets,
                "--port",
                str(FANOUT_PORT),
                "--count",
                str(count),
                "--size",
                str(size),
                "--period",
                period,
                "--seed",
                seed_for(aggregator),
            ),
        )
    )
    return processes
