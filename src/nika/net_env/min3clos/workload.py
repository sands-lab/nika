"""Business traffic of the min3clos fabric (always on, every case).

The two clients are servers in different racks:

- **Replication.** Each client publishes a data set over HTTP (port 8080);
  client2 pulls client1's at 2 Mbit/s and client1 pulls client2's at
  1 Mbit/s, back to back.
- **Sessions.** client2 runs a web/push service; client1 keeps persistent
  sessions and page loads to it.

Both stay far below the fabric's capacity, so a healthy lab shows no loss or
queueing.
"""

from __future__ import annotations

from nika.traffic.workload import (
    BULK_DOWNLOAD,
    BULK_TRANSIT,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
)

BULK_PORT = 8080
CLIENT_IP = {"client1": "10.0.0.25", "client2": "10.0.0.27"}
# (server, client, kbit/s)
REPLICATION = (("client1", "client2", 2000), ("client2", "client1", 1000))
# Egress hops of client1 -> client2, which carries the 2 Mbit/s replication,
# with router ports as SR Linux names them.
CLIENT1_TO_CLIENT2 = (
    ("client1", "eth1"),
    ("leaf1", "e1-1"),
    ("spine", "e1-2"),
    ("leaf2", "e1-2"),
)


def build_workload(router_port=lambda port: port) -> BusinessWorkload:
    """``router_port`` maps an SR Linux port name to the deployed router's."""
    processes: list[WorkloadProcess] = []
    exercises: list[WorkloadExercise] = []
    for server, client, kbps in REPLICATION:
        processes.append(
            WorkloadProcess(
                node=server,
                app="file_server",
                command="filedist",
                args=("--root", "/var/www", "--port", str(BULK_PORT)),
            )
        )
        url = f"http://{CLIENT_IP[server]}:{BULK_PORT}/large.bin?rate={kbps}k"
        processes.append(
            WorkloadProcess(
                node=client,
                app="file_sync",
                command="filesync",
                args=(url, "--pause", "1-3", "--seed", str(kbps)),
            )
        )
        exercises.append(WorkloadExercise(BULK_DOWNLOAD, (client,)))
    exercises += [
        WorkloadExercise(
            BULK_TRANSIT,
            (node, port if node.startswith("client") else router_port(port)),
        )
        for node, port in CLIENT1_TO_CLIENT2
    ]
    processes += [
        WorkloadProcess(
            node="client2",
            app="web_service",
            command="portal",
            args=("--port", "80", "--seed", "2"),
        ),
        WorkloadProcess(
            node="client1",
            app="web_client",
            command="webclient",
            args=(
                CLIENT_IP["client2"],
                "--sessions", "4", "--lifetime", "120-600",
                "--burst", "3", "--burst-every", "20-40", "--burst-kb", "8-64",
                "--seed", "1",
            ),
        ),
    ]  # fmt: skip
    return BusinessWorkload(processes=tuple(processes), exercises=tuple(exercises))
