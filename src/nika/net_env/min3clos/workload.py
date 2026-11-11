"""Business traffic of the min3clos fabric (always on, every case).

The two clients are servers in different racks; see
:mod:`nika.net_env.utils.pair_workload`. client1 is the first host, so its
2 Mbit/s replication to client2 crosses the fabric leaf1 -> spine -> leaf2.
"""

from __future__ import annotations

from nika.net_env.utils.pair_workload import build_pair_workload
from nika.traffic.workload import BusinessWorkload

CLIENT_IP = {"client1": "10.0.0.25", "client2": "10.0.0.27"}
# Egress hops of client1 -> client2, with router ports as SR Linux names them.
CLIENT1_TO_CLIENT2 = (
    ("client1", "eth1"),
    ("leaf1", "e1-1"),
    ("spine", "e1-2"),
    ("leaf2", "e1-2"),
)


def build_workload(router_port=lambda port: port) -> BusinessWorkload:
    """``router_port`` maps an SR Linux port name to the deployed router's."""
    return build_pair_workload(
        ("client1", CLIENT_IP["client1"]),
        ("client2", CLIENT_IP["client2"]),
        tuple(
            (node, port if node.startswith("client") else router_port(port))
            for node, port in CLIENT1_TO_CLIENT2
        ),
    )
