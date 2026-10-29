"""SONiC provider for failure operations on Containerlab SONiC-VS routers.

Each operation changes the layer SONiC itself keeps the setting in: routing
in FRR (``vtysh``, and ``/etc/frr/frr.conf`` where a restart would read it),
port settings in CONFIG_DB, and control-plane filters in ``iptables``, which
is where SONiC's control-plane ACL service installs them. Failures name
container interfaces (``eth{k}``); SONiC ports are ``Ethernet{4(k-1)}``.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex

from nika.net_env.utils.sonic.common import container_iface, sonic_port
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
# OSPF (IP protocol 89) only reaches or leaves the router's own stack.
_OSPF_CHAINS = ("INPUT", "OUTPUT")
_OSPF_DROP = "-p ospf -j DROP"
_OSPF_NETWORK = re.compile(r"^\s+network (\S+) area (\S+)$", re.M)


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

    def frr_shell(self, command: str) -> str:
        # SONiC routes with FRR in the same namespace as its ports.
        return self._exec(command, timeout=60)

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

    def _ospf_interface_addresses(self) -> dict[str, str]:
        """Map each OSPF-enabled port to its interface address."""
        data = json.loads(self._vtysh("show ip ospf interface json") or "{}")
        return {
            name: str(info["ipAddress"])
            for name, info in (data.get("interfaces") or {}).items()
            if name.startswith("Ethernet") and info.get("ipAddress")
        }

    def ospf_interfaces(self) -> list[str]:
        return sorted(map(container_iface, self._ospf_interface_addresses()))

    def ospf_neighbor_interfaces(self) -> list[str]:
        data = json.loads(self._vtysh("show ip ospf neighbor json") or "{}")
        ports = [
            str(entry.get("ifaceName", "")).split(":", 1)[0]
            for entries in (data.get("neighbors") or {}).values()
            for entry in (entries if isinstance(entries, list) else [entries])
            if str(entry.get("nbrState", "")).startswith("Full")
        ]
        return sorted(container_iface(port) for port in ports if port)

    def remove_ospf_interface(self, interface: str) -> None:
        # The design enables OSPF with ``network`` statements: drop the one
        # that covers the port from the daemon and from frr.conf.
        address = self._ospf_interface_addresses().get(self._port(interface))
        if address is None:
            raise ValueError(f"OSPF does not run on {self.node}:{interface}")
        ip = ipaddress.ip_address(address)
        for prefix, area in _OSPF_NETWORK.findall(self._ospf_block()):
            if ip in ipaddress.ip_network(prefix, strict=False):
                self._configure("router ospf", f"no network {prefix} area {area}")
                line = f" network {prefix} area {area}"
                self._exec(
                    f"grep -vxF {shlex.quote(line)} {_FRR_CONF} > {_FRR_CONF}.new"
                )
                self._exec(f"cat {_FRR_CONF}.new > {_FRR_CONF}; rm -f {_FRR_CONF}.new")
                return
        raise ValueError(f"No OSPF network covers {self.node}:{interface}")

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

    def block_ospf(self) -> None:
        for chain in _OSPF_CHAINS:
            self._exec(f"iptables -I {chain} {_OSPF_DROP}")

    def ospf_blocked(self) -> bool:
        return all(self._rule_present(chain, _OSPF_DROP) for chain in _OSPF_CHAINS)

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

    def interface_up(self, interface: str) -> bool:
        port = self._port(interface)
        status = self._exec(f"redis-cli -n 0 hget 'PORT_TABLE:{port}' oper_status")
        return status.strip() == "up"

    def _config_rows(self, pattern: str) -> dict[str, dict[str, str]]:
        keys = self._exec(f"redis-cli -n 4 --raw keys {shlex.quote(pattern)}").split()
        rows = {}
        for key in keys:
            values = self._exec(f"redis-cli -n 4 --raw hgetall {shlex.quote(key)}")
            fields = values.splitlines()
            rows[key] = dict(zip(fields[::2], fields[1::2]))
        return rows

    def _wait_port(self, port: str, *, present: bool) -> None:
        test = "" if present else "!"
        self._exec(
            f"for _ in $(seq 1 60); do {test} ip link show {port} >/dev/null 2>&1 "
            "&& break; sleep 1; done",
            timeout=90,
        )

    def reattach_interface(self, interface: str) -> None:
        # The virtual switch binds a port to its container interface when it
        # creates the port, so the port is removed and created again from its
        # own CONFIG_DB rows; addresses go after the port and before it.
        port = self._port(interface)
        rows = self._config_rows(f"PORT|{port}")
        rows.update(self._config_rows(f"INTERFACE|{port}"))
        rows.update(self._config_rows(f"INTERFACE|{port}|*"))
        order = sorted(rows, key=lambda key: (not key.startswith("PORT|"), key))
        for key in reversed(order):
            self._exec(f"redis-cli -n 4 del {shlex.quote(key)}")
            if key.startswith("PORT|"):
                self._wait_port(port, present=False)
        for key in order:
            fields = " ".join(
                shlex.quote(item) for pair in rows[key].items() for item in pair
            )
            self._exec(f"redis-cli -n 4 hset {shlex.quote(key)} {fields}")
            if key.startswith("PORT|"):
                self._wait_port(port, present=True)

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
