from __future__ import annotations

from nika.net_env.kathara.interdomain_routing.iosxr_simple_bgp.verify import (
    verify_iosxr_simple_bgp_lab,
)
from tests.support.net_env import assert_verify_success

NODES = {"router1", "router2", "pc1", "pc2"}
HOST_ADDRS = {
    "pc1": ("195.11.14.2",),
    "pc2": ("200.1.1.2",),
}


class FakeRuntime:
    def __init__(self, *, nodes: set[str] | None = None) -> None:
        self.nodes = nodes or NODES

    def list_nodes(self) -> list[str]:
        return sorted(self.nodes)

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        if command == "/pkg/bin/xr_cli 'show ip interface brief'":
            return "GigabitEthernet0/0/0/0 10.0.0.1 Up Up default"
        if command == "/pkg/bin/xr_cli 'show bgp summary'":
            return "193.10.11.2 4 2 10 12 5 0 0 00:02:15 3\n"
        if command.startswith("ping -c 1"):
            return "1 received"
        if command.startswith("ip -4 -o addr show"):
            return "\n".join(f"inet {addr}/24" for addr in HOST_ADDRS.get(host, ()))
        if command == "ip route show default":
            return "default via 195.11.14.1 dev eth0"
        return ""


def test_iosxr_simple_bgp_verify_passes() -> None:
    assert_verify_success(verify_iosxr_simple_bgp_lab(FakeRuntime(), scenario_name="x"))


def test_iosxr_simple_bgp_verify_fails_on_missing_node() -> None:
    result = verify_iosxr_simple_bgp_lab(
        FakeRuntime(nodes=NODES - {"pc2"}), scenario_name="x"
    )
    assert not result["verified"]
    assert not result["checks"]["nodes_deployed"]
