"""Site Edge observations used by enterprise_branch verification.

Each method returns the command that reads one piece of edge state; the
``*_ok`` and ``qos_checks`` methods interpret the output. ``FrrEdgeView``
reads FRR and Linux state; vendor profiles subclass it with native commands.
"""

from __future__ import annotations

from nika.net_env.enterprise_branch.topology import OverlayQosSpec
from nika.net_env.verify import FRR_BGP_PEERS, BgpPeerQuery, ping_ok
from nika.net_env.verify_collection import ping_command


class FrrEdgeView:
    bgp_peers: BgpPeerQuery = FRR_BGP_PEERS
    routing_process = "pgrep -x bgpd"
    bgp_rib = "vtysh -c 'show ip bgp'"
    vrf_list = "ip -d link show type vrf"

    def routing_process_ok(self, edge: str, output: str) -> bool:
        return bool(output.strip())

    def wireguard(self, iface: str) -> str:
        return f"wg show {iface}"

    def wireguard_ok(self, iface: str, output: str) -> bool:
        return bool(output.strip())

    def vrf_ribs(self, vrf: str, *, routing_table_first: bool) -> tuple[str, ...]:
        """Commands tried in order until one returns output."""
        kernel = f"ip route show vrf {vrf}"
        if routing_table_first:
            return (f"vtysh -c 'show ip route vrf {vrf}'", kernel)
        return (kernel,)

    def route_lookup(self, vrf: str, address: str, prefix: str) -> str:
        return f"ip route get {address} vrf {vrf}"

    def overlay_path_ok(self, output: str) -> bool:
        return "dev wg" in output or "172.30." in output

    def bgp_route(self, prefix: str) -> str:
        return f"vtysh -c 'show ip bgp {prefix}'"

    def qos(self, iface: str) -> tuple[str, ...]:
        return (f"tc qdisc show dev {iface}", f"tc class show dev {iface}")

    def qos_checks(
        self, outputs: tuple[str, ...], qos: OverlayQosSpec
    ) -> dict[str, bool]:
        tc_out, tc_class = outputs
        return {
            "htb": "htb" in tc_out.lower(),
            "ef_class": "1:10" in tc_class,
            "be_class": "1:20" in tc_class,
            "rate": f"{qos.rate_mbit}Mbit" in tc_class
            or (f"{qos.rate_mbit}mbit" in tc_class.lower()),
        }

    def ping(self, target: str) -> str:
        return ping_command(target)

    def ping_ok(self, runtime, edge: str, target: str) -> bool:
        return ping_ok(runtime, edge, target)
