"""SONiC provider for failure operations on Containerlab SONiC-VS routers.

Each operation changes the layer SONiC itself keeps the setting in: routing
in FRR (``vtysh``, and ``/etc/frr/frr.conf`` where a restart would read it),
port settings in CONFIG_DB, and control-plane filters in ``iptables``, which
is where SONiC's control-plane ACL service installs them. Failures name
container interfaces (``eth{k}``); SONiC ports are ``Ethernet{4(k-1)}``.
"""

from __future__ import annotations

import json
import re
import shlex

from nika.net_env.utils.sonic.common import sonic_port
from nika.net_env.verify import frr_bgp_established_peers
from nika.problems.support.router_ops import RouterFaultOps

_FRR_CONF = "/etc/frr/frr.conf"
_ROUTING_DAEMONS = ("bgpd", "ospfd", "isisd", "staticd", "zebra", "mgmtd")
_BGP_RULES = tuple(
    (chain, f"--{side} 179")
    for chain in ("INPUT", "OUTPUT")
    for side in ("dport", "sport")
)
_FRAG_NEEDED_CHAINS = ("INPUT", "FORWARD", "OUTPUT")
_FRAG_NEEDED = "-p icmp --icmp-type fragmentation-needed -j DROP"


class SonicFaultOps(RouterFaultOps):
    profile = "sonic"

    def _exec(self, command: str, timeout: float = 30) -> str:
        return self.runtime.exec(self.node, command, timeout=timeout)

    def _vtysh(self, *lines: str) -> str:
        args = " ".join(f"-c {shlex.quote(line)}" for line in lines)
        return self._exec(f"vtysh {args}")

    def _configure(self, *lines: str) -> str:
        return self._vtysh("configure terminal", *lines)

    def _running(self) -> str:
        return self._vtysh("show running-config")

    def _port(self, interface: str) -> str:
        return interface if interface.startswith("Ethernet") else sonic_port(interface)

    # BGP

    def bgp_asn(self) -> int:
        match = re.search(r"^router bgp (\d+)", self._running(), re.MULTILINE)
        if not match:
            raise ValueError(f"No BGP instance on SONiC router {self.node!r}")
        return int(match[1])

    def set_bgp_asn(self, asn: int) -> None:
        # FRR cannot renumber a live instance: remove it and reload the
        # configuration file, which keeps it in agreement with the router.
        current = self.bgp_asn()
        self._exec(
            f"sed -i -E 's/^router bgp {current}( |$)/router bgp {int(asn)}\\1/' "
            f"{_FRR_CONF}"
        )
        self._configure(f"no router bgp {current}")
        self._exec(f"vtysh -f {_FRR_CONF}", timeout=60)

    def bgp_networks(self) -> list[str]:
        return sorted(set(re.findall(r"^\s+network (\S+)", self._bgp_block(), re.M)))

    def _bgp_block(self) -> str:
        match = re.search(
            r"^router bgp \d+\n(.*?)^(?:exit|!)$", self._running(), re.M | re.S
        )
        return match[1] if match else ""

    def withdraw_bgp_network(self, prefix: str) -> None:
        self._configure(
            f"router bgp {self.bgp_asn()}",
            "address-family ipv4 unicast",
            f"no network {prefix}",
        )

    def bgp_established_peers(self) -> set[str]:
        return frr_bgp_established_peers(self._vtysh("show bgp summary json"))

    def remote_bgp_prefixes(self) -> list[str]:
        routes = json.loads(self._vtysh("show ip route bgp json") or "{}")
        return sorted(
            prefix
            for prefix, entries in routes.items()
            if any(entry.get("selected") for entry in entries)
        )

    # Routing process

    def stop_routing_process(self) -> None:
        # A crashed routing container takes every FRR daemon with it.
        for daemon in _ROUTING_DAEMONS:
            self._exec(f"pkill -9 -x {daemon} || true")

    # OSPF

    def ospf_interface_areas(self) -> list[str]:
        return re.findall(r"^\s+network \S+ area (\S+)", self._ospf_block(), re.M)

    def _ospf_block(self) -> str:
        match = re.search(
            r"^router ospf\n(.*?)^(?:exit|!)$", self._running(), re.M | re.S
        )
        return match[1] if match else ""

    def renumber_ospf_area(self, area: str, new_area: str) -> None:
        networks = re.findall(
            rf"^\s+network (\S+) area {re.escape(area)}$", self._ospf_block(), re.M
        )
        lines = ["router ospf"]
        for network in networks:
            lines += [f"no network {network} area {area}"]
            lines += [f"network {network} area {new_area}"]
        self._configure(*lines)

    # Packet filters

    def _rule_present(self, chain: str, rule: str) -> bool:
        return "present" in self._exec(
            f"iptables -C {chain} {rule} 2>/dev/null && echo present"
        )

    def block_bgp_transport(self) -> None:
        for chain, match in _BGP_RULES:
            self._exec(f"iptables -I {chain} -p tcp {match} -j DROP")
        # Drop the sessions now instead of after the hold timer.
        self._vtysh("clear ip bgp *")

    def bgp_transport_blocked(self) -> bool:
        return all(
            self._rule_present(chain, f"-p tcp {match} -j DROP")
            for chain, match in _BGP_RULES
        )

    def block_icmp_frag_needed(self) -> None:
        for chain in _FRAG_NEEDED_CHAINS:
            self._exec(f"iptables -I {chain} {_FRAG_NEEDED}")

    def icmp_frag_needed_blocked(self) -> bool:
        return all(
            self._rule_present(chain, _FRAG_NEEDED) for chain in _FRAG_NEEDED_CHAINS
        )

    # Routes and interfaces

    def add_blackhole_route(self, prefix: str) -> None:
        self._configure(f"ip route {prefix} blackhole")

    def blackhole_routes(self) -> list[str]:
        return sorted(
            set(re.findall(r"^ip route (\S+) blackhole", self._running(), re.M))
        )

    def set_interface_mtu(self, interface: str, mtu: int) -> None:
        # portmgrd applies the CONFIG_DB port MTU to the kernel port.
        self._exec(f"redis-cli -n 4 hset 'PORT|{self._port(interface)}' mtu {int(mtu)}")

    def interface_mtu(self, interface: str) -> int | None:
        port = self._port(interface)
        configured = self._exec(f"redis-cli -n 4 hget 'PORT|{port}' mtu").strip()
        kernel = self._exec(f"cat /sys/class/net/{port}/mtu 2>/dev/null").strip()
        # Both CONFIG_DB and the kernel port must carry it.
        return int(kernel) if configured.isdigit() and configured == kernel else None


FAULT_OPS = SonicFaultOps
