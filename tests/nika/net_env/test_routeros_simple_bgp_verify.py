from __future__ import annotations

from nika.net_env.kathara.interdomain_routing.routeros_simple_bgp.verify import (
    CLI_COMMAND,
    verify_routeros_simple_bgp_lab,
)
from tests.support.net_env import assert_verify_success

NODES = {"router1", "router2", "pc1", "pc2"}
HOST_ADDRS = {
    "pc1": ("195.11.14.2",),
    "pc2": ("200.1.1.2",),
}

IFACE_CMD = CLI_COMMAND.format(
    command="/interface print terse where name=ether2 running=yes"
)
BGP_CMD = CLI_COMMAND.format(
    command="/routing/bgp/session/print terse where established=yes"
)


class FakeRuntime:
    def __init__(self, *, nodes: set[str] | None = None) -> None:
        self.nodes = nodes or NODES

    def list_nodes(self) -> list[str]:
        return sorted(self.nodes)

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        if command == IFACE_CMD:
            return "0 R ether2\n"
        if command == BGP_CMD:
            return "0 name=to-peer remote.address=193.10.11.2\n"
        if command.startswith("ping -c 1"):
            return "1 received"
        if command.startswith("ip -4 -o addr show"):
            return "\n".join(f"inet {addr}/24" for addr in HOST_ADDRS.get(host, ()))
        if command == "ip route show default":
            return "default via 195.11.14.1 dev eth0"
        return ""


def test_routeros_simple_bgp_verify_passes() -> None:
    assert_verify_success(
        verify_routeros_simple_bgp_lab(FakeRuntime(), scenario_name="x")
    )


def test_routeros_simple_bgp_verify_fails_on_missing_node() -> None:
    result = verify_routeros_simple_bgp_lab(
        FakeRuntime(nodes=NODES - {"pc2"}), scenario_name="x"
    )
    assert not result["verified"]
    assert not result["checks"]["nodes_deployed"]
