"""SONiC provider for failure operations on Containerlab SONiC-VS routers.

Each operation changes the layer SONiC itself keeps the setting in: routing
in FRR (``vtysh``, and ``/etc/frr/frr.conf`` where a restart would read it),
port settings in CONFIG_DB, and control-plane filters in ``iptables``, which
is where SONiC's control-plane ACL service installs them. Failures name
container interfaces (``eth{k}``); SONiC ports are ``Ethernet{4(k-1)}``.
"""

from __future__ import annotations

from collections import Counter
import base64
import ipaddress
import json
import re
import shlex

from nika.net_env.utils.sonic.common import container_iface, sonic_port
from nika.net_env.verify import frr_bgp_established_peers
from nika.problems.support.frr import FRR_SAVE_COMMAND
from nika.problems.support.ospf import router_lsa_origins
from nika.problems.support.recovery import checked_exec
from nika.problems.support.router_ops import RouterFaultOps, OspfNeighbor

_FRR_CONF = "/etc/frr/frr.conf"
# SONiC name of the loopback that holds BGP-only prefixes (Kathara ``lo1``).
_ORIGIN_LOOPBACK = "Loopback1"
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
        self._exec(FRR_SAVE_COMMAND)

    def originate_bgp_prefix(self, prefix: str) -> None:
        net = ipaddress.ip_network(prefix, strict=False)
        address = net.network_address if net.prefixlen >= 31 else next(net.hosts())
        # The router answers for the prefix from a loopback, where the ISP
        # design keeps its own BGP-only prefixes; intfmgrd creates the
        # loopback and applies the address from CONFIG_DB.
        interface = f"{_ORIGIN_LOOPBACK}|{address}/{net.prefixlen}"
        for key in (_ORIGIN_LOOPBACK, interface):
            db_key = f"LOOPBACK_INTERFACE|{key}"
            if self._exec(f"redis-cli -n 4 exists '{db_key}'").strip() != "1":
                self.__dict__.setdefault("_origin_keys", []).append(db_key)
                self._exec(f"redis-cli -n 4 hset '{db_key}' NULL NULL")
        self._exec(
            "for _ in $(seq 1 30); do "
            f"ip -4 addr show dev {_ORIGIN_LOOPBACK} 2>/dev/null "
            f"| grep -qF ' {address}/' && break; sleep 1; done",
            timeout=45,
        )
        seen = self._exec(
            f"ip -4 addr show dev {_ORIGIN_LOOPBACK} 2>/dev/null "
            f"| grep -qF ' {address}/' && echo present"
        )
        if "present" not in seen:
            raise RuntimeError(
                f"SONiC {_ORIGIN_LOOPBACK} on {self.node} did not get "
                f"{address}/{net.prefixlen}"
            )
        # A hijacked prefix must terminate traffic for its interior addresses,
        # as the FRR implementation's local-prefix route does.
        previous = self._exec(f"ip -4 route show table local exact {net}").strip()
        self.__dict__.setdefault("_origin_routes", {}).setdefault(str(net), previous)
        self._exec(f"ip route replace local {net} dev {_ORIGIN_LOOPBACK} table local")
        self._configure(
            f"router bgp {self.bgp_asn()}",
            "address-family ipv4 unicast",
            f"network {net}",
        )
        # Kept in frr.conf too, as an operator's saved change would be.
        self._exec(FRR_SAVE_COMMAND)

    def bgp_advertised_to(self, prefix: str) -> list[str]:
        entry = json.loads(self._vtysh(f"show bgp ipv4 unicast {prefix} json") or "{}")
        # Only the router's own origination counts, not a path it relays.
        sourced = any(
            path.get("sourced") and (path.get("bestpath") or {}).get("overall")
            for path in entry.get("paths") or []
        )
        return sorted(entry.get("advertisedTo") or {}) if sourced else []

    def bgp_established_peers(self) -> set[str]:
        return frr_bgp_established_peers(self._vtysh("show bgp summary json"))

    def set_bgp_password(self, peer: str, password: str | None) -> None:
        line = (
            f"neighbor {peer} password {password}"
            if password is not None
            else f"no neighbor {peer} password"
        )
        self._configure(f"router bgp {self.bgp_asn()}", line)
        self._exec(FRR_SAVE_COMMAND)

    def bgp_password_configured(self, peer: str) -> bool:
        return bool(
            re.search(
                rf"^\s+neighbor {re.escape(peer)} password .+$", self._bgp_block(), re.M
            )
        )

    def reset_bgp_session(self, peer: str) -> None:
        self._vtysh(f"clear bgp {peer}")

    def bgp_received_prefixes(self, peer: str) -> set[str]:
        data = json.loads(
            self._vtysh(f"show bgp ipv4 unicast neighbors {peer} routes json")
        )
        if not isinstance(data.get("routes"), dict):
            raise ValueError(f"Unreadable BGP routes on {self.node!r}: {data}")
        return set(data["routes"])

    def _bgp_peers(self) -> set[str]:
        return set(
            re.findall(r"^\s+neighbor (\S+) remote-as ", self._bgp_block(), re.M)
        )

    def enable_bgp_ttl_security(self) -> None:
        self._configure(
            f"router bgp {self.bgp_asn()}",
            *(
                f"neighbor {peer} ttl-security hops 1"
                for peer in sorted(self._bgp_peers())
            ),
        )
        self._exec(FRR_SAVE_COMMAND)
        self.reset_bgp()

    def bgp_ttl_security_enabled(self) -> bool:
        peers = self._bgp_peers()
        secured = set(
            re.findall(
                r"^\s+neighbor (\S+) ttl-security hops 1$", self._bgp_block(), re.M
            )
        )
        return bool(peers) and peers <= secured

    def reset_bgp(self) -> None:
        self._vtysh("clear bgp *")

    def remote_bgp_prefixes(self) -> list[str]:
        routes = json.loads(self._vtysh("show ip route bgp json") or "{}")
        return sorted(
            prefix
            for prefix, entries in routes.items()
            if any(entry.get("selected") for entry in entries)
        )

    # Routing process

    def stop_routing_process(self) -> None:
        # Stop supervision too, so a dead daemon stays down.
        running = [name for name, up in self.routing_process_status().items() if up]
        if running:
            self._exec("supervisorctl stop " + " ".join(running), timeout=90)

    def routing_process_status(self) -> dict[str, bool]:
        output = self._exec("supervisorctl status")
        status = {
            fields[0]: fields[1] == "RUNNING"
            for line in output.splitlines()
            if len(fields := line.split()) >= 2 and fields[0] in _ROUTING_DAEMONS
        }
        if "zebra" not in status:
            raise RuntimeError(
                f"SONiC routing process status unavailable on {self.node}"
            )
        return status

    def start_routing_process(self, saved: dict[str, bool]) -> None:
        current = self.routing_process_status()
        start = [name for name, up in saved.items() if up and not current.get(name)]
        if start:
            # Protocol daemons connect to zebra when they start.
            start.sort(key=lambda name: (name != "zebra", name))
            self._exec("supervisorctl start " + " ".join(start), timeout=90)
            output = checked_exec(self.runtime, self.node, "vtysh -b")
            if re.search(
                r"failed to connect|not running|connection refused|%|\[TIMEOUT\]",
                output,
                re.I,
            ):
                raise RuntimeError(
                    f"SONiC routing configuration reload failed on {self.node}: {output}"
                )

    # OSPF

    def connected_routes(self) -> set[str]:
        return set(json.loads(self._vtysh("show ip route connected json")))

    def ospf_router_id(self) -> str | None:
        return json.loads(self._vtysh("show ip ospf json")).get("routerId")

    def ospf_neighbor_ids(self) -> set[str]:
        return {neighbor.router_id for neighbor in self.ospf_neighbors()}

    def ospf_router_lsa_ids(self) -> set[str]:
        origins = router_lsa_origins(self._vtysh("show ip ospf database"))
        return set().union(*origins.values())

    def set_ospf_router_id(self, router_id: str) -> None:
        self._configure("router ospf", f"ospf router-id {router_id}")
        self._vtysh("clear ip ospf process")
        self._exec(FRR_SAVE_COMMAND)

    def _ospf_interface(self, interface: str) -> dict:
        port = self._port(interface)
        data = json.loads(self._vtysh("show ip ospf interface json"))
        info = data.get("interfaces", {}).get(port)
        if not info:
            raise ValueError(f"OSPF does not run on {self.node}:{interface} ({port})")
        return info

    def ospf_interface_neighbor_full(self, interface: str) -> bool:
        return any(
            neighbor.interface == container_iface(self._port(interface))
            and neighbor.state.lower().startswith("full")
            for neighbor in self.ospf_neighbors()
        )

    def ospf_interface_timers(self, interface: str) -> tuple[int, int]:
        data = self._ospf_interface(interface)
        hello_ms = int(data["timerMsecs"])
        if hello_ms <= 0 or hello_ms % 1000:
            raise ValueError(
                f"Unsupported OSPF hello interval {hello_ms} ms on "
                f"{self.node}:{interface}; expected positive whole seconds"
            )
        return hello_ms // 1000, int(data["timerDeadSecs"])

    def set_ospf_interface_timers(self, interface: str, hello: int, dead: int) -> None:
        self._configure(
            f"interface {self._port(interface)}",
            f"ip ospf hello-interval {hello}",
            f"ip ospf dead-interval {dead}",
        )
        self._exec(FRR_SAVE_COMMAND)

    def ospf_network_type(self, interface: str) -> str:
        token = self._ospf_interface(interface)["networkType"]
        types = {
            "BROADCAST": "broadcast",
            "POINTOPOINT": "point-to-point",
            "POINTOMULTIPOINT": "point-to-multipoint",
            "NBMA": "non-broadcast",
        }
        if token not in types:
            raise ValueError(
                f"Unsupported OSPF network type {token!r} on {self.node}:{interface}"
            )
        return types[token]

    def set_ospf_network_type(self, interface: str, network_type: str) -> None:
        self._configure(
            f"interface {self._port(interface)}", f"ip ospf network {network_type}"
        )
        self._exec(FRR_SAVE_COMMAND)

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
        self._exec(FRR_SAVE_COMMAND)

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

    def ipv4_addresses(self) -> set[str]:
        data = json.loads(self._exec("ip -j -4 address show"))
        return {
            address["local"]
            for row in data
            if row["ifname"].startswith(("Ethernet", "Loopback"))
            for address in row.get("addr_info", [])
            if address.get("family") == "inet"
        }

    def ospf_neighbors(self) -> list[OspfNeighbor]:
        data = json.loads(self._vtysh("show ip ospf neighbor json"))
        return [
            OspfNeighbor(
                router_id,
                entry["ifaceAddress"],
                container_iface(entry["ifaceName"].split(":", 1)[0]),
                entry["nbrState"],
            )
            for router_id, entries in data.get("neighbors", {}).items()
            for entry in (entries if isinstance(entries, list) else [entries])
        ]

    def ospf_bindings(self) -> dict[str, tuple[str, ...]]:
        return {
            f"network {prefix} area {area}": tuple(
                container_iface(port)
                for port, address in self._ospf_interface_addresses().items()
                if ipaddress.ip_address(address)
                in ipaddress.ip_network(prefix, strict=False)
            )
            for prefix, area in _OSPF_NETWORK.findall(self._ospf_block())
        }

    def remove_ospf_binding(self, binding: str) -> None:
        if binding not in self.ospf_bindings():
            raise ValueError(f"No OSPF binding {binding!r} on {self.node}")
        self._configure("router ospf", f"no {binding}")
        self._exec(FRR_SAVE_COMMAND)

    def ospf_routes(self) -> set[str]:
        return set(json.loads(self._vtysh("show ip route ospf json")))

    def save_config(self) -> str:
        """Capture the configuration domains changed by native fault operations."""
        links = json.loads(self._exec("ip -j link show"))
        return json.dumps(
            {
                "frr": self._running(),
                "mtu": {
                    row["ifname"]: row["mtu"]
                    for row in links
                    if row["ifname"].startswith("Ethernet")
                },
            }
        )

    def restore_config(self, config: str) -> None:
        """Restore routing, packet filters, and changed port MTUs from a snapshot."""
        from nika.problems.support.polling import wait_until

        saved = json.loads(config)
        current = self._running()
        if current != saved["frr"]:
            # Remove changed protocol/policy instances before loading their saved
            # definitions; applying a file alone cannot undo deleted bindings or
            # remove an injected route, instance, or prefix-list entry.
            pattern = (
                r"^((?:router (?:bgp|ospf|isis)\b|route-map\b)[^\n]*)\n.*?(?=^!\s*$|\Z)"
            )
            before = {
                match[1]: match[0]
                for match in re.finditer(pattern, saved["frr"], re.M | re.S)
            }
            after = {
                match[1]: match[0]
                for match in re.finditer(pattern, current, re.M | re.S)
            }
            remove = [
                f"no {header}"
                for header, block in after.items()
                if before.get(header) != block
            ]
            statements = (
                r"^(?:ip route |ipv6 route |ip prefix-list |ipv6 prefix-list ).*$"
            )
            remove += [
                f"no {line}"
                for line in (
                    Counter(re.findall(statements, current, re.M))
                    - Counter(re.findall(statements, saved["frr"], re.M))
                ).elements()
            ]
            if remove:
                self._configure(*remove)
            encoded = base64.b64encode(saved["frr"].encode()).decode()
            self._exec(f"printf %s {shlex.quote(encoded)} | base64 -d > {_FRR_CONF}")
            self._exec(f"vtysh -f {_FRR_CONF}", timeout=60)
            self._exec(FRR_SAVE_COMMAND)
        # Keep rules installed before or after this operation. The provider
        # instance retained by recovery tracks only its own insertions.
        for chain, rule in self.__dict__.pop("_inserted_filter_rules", []):
            if self._rule_present(chain, rule):
                self._exec(f"iptables -D {chain} {rule}")
        for interface in self.__dict__.pop("_changed_mtu", set()):
            mtu = saved["mtu"][interface]
            if self.interface_mtu(interface) != mtu:
                self._exec(
                    f"redis-cli -n 4 hset 'PORT|{self._port(interface)}' mtu {int(mtu)}"
                )
                if not wait_until(
                    lambda: self.interface_mtu(interface) == mtu, 30, interval_s=1
                ):
                    raise RuntimeError(
                        f"SONiC MTU recovery failed on {self.node}:{interface}"
                    )
        for prefix, previous in self.__dict__.pop("_origin_routes", {}).items():
            if previous:
                self._exec(f"ip route replace table local {previous}")
            else:
                self._exec(f"ip route del local {prefix} table local")
        for key in reversed(self.__dict__.pop("_origin_keys", [])):
            self._exec(f"redis-cli -n 4 del {shlex.quote(key)}")

    # IS-IS

    def _isis_block(self) -> tuple[str, str]:
        match = re.search(
            r"^router isis (\S+)\n(.*?)(?=^\S|\Z)", self._running(), re.M | re.S
        )
        if match is None:
            raise ValueError(f"No IS-IS instance on SONiC router {self.node!r}")
        return match[1], match[2]

    def isis_metric_style(self) -> str:
        match = re.search(r"^ metric-style (\S+)$", self._isis_block()[1], re.M)
        return match[1] if match else "wide"

    def set_isis_metric_style(self, style: str) -> None:
        self._configure(f"router isis {self._isis_block()[0]}", f"metric-style {style}")
        self._exec(FRR_SAVE_COMMAND)

    def isis_level(self) -> str:
        match = re.search(r"^ is-type (\S+)$", self._isis_block()[1], re.M)
        return match[1] if match else "level-1-2"

    def set_isis_level(self, level: str) -> None:
        self._configure(f"router isis {self._isis_block()[0]}", f"is-type {level}")
        self._exec(FRR_SAVE_COMMAND)

    def isis_hello_authentication(self) -> dict[str, str]:
        circuits = {}
        for interface, block in re.findall(
            r"^interface (\S+)[^\n]*\n(.*?)(?=^\S|\Z)", self._running(), re.M | re.S
        ):
            if not re.search(r"^ ip router isis \S+", block, re.M):
                continue
            if re.search(r"^ isis passive\s*$", block, re.M):
                continue
            auth = re.search(r"^ isis password (\S+)", block, re.M)
            circuits[container_iface(interface)] = auth[1] if auth else "none"
        return circuits

    def set_isis_hello_password(self, password: str | None) -> None:
        lines = []
        for interface in self.isis_hello_authentication():
            lines.extend(
                [
                    f"interface {self._port(interface)}",
                    f"isis password md5 {password}" if password else "no isis password",
                    "exit",
                ]
            )
        self._configure(*lines)
        self._exec(FRR_SAVE_COMMAND)

    # Packet filters

    def _insert_filter_rule(self, chain: str, rule: str) -> None:
        self._exec(f"iptables -I {chain} {rule}")
        self.__dict__.setdefault("_inserted_filter_rules", []).append((chain, rule))

    def _rule_present(self, chain: str, rule: str) -> bool:
        return "present" in self._exec(
            f"iptables -C {chain} {rule} 2>/dev/null && echo present"
        )

    def block_bgp_transport(self) -> None:
        for chain, match in _BGP_RULES:
            self._insert_filter_rule(chain, f"-p tcp {match} -j DROP")
        # Drop the sessions now instead of after the hold timer.
        self._vtysh("clear ip bgp *")

    def bgp_transport_blocked(self) -> bool:
        return all(
            self._rule_present(chain, f"-p tcp {match} -j DROP")
            for chain, match in _BGP_RULES
        )

    def block_ospf(self) -> None:
        for chain in _OSPF_CHAINS:
            self._insert_filter_rule(chain, _OSPF_DROP)

    def ospf_blocked(self) -> bool:
        return all(self._rule_present(chain, _OSPF_DROP) for chain in _OSPF_CHAINS)

    def block_icmp_frag_needed(self) -> None:
        for chain in _FRAG_NEEDED_CHAINS:
            self._insert_filter_rule(chain, _FRAG_NEEDED)

    def icmp_frag_needed_blocked(self) -> bool:
        return all(
            self._rule_present(chain, _FRAG_NEEDED) for chain in _FRAG_NEEDED_CHAINS
        )

    # Routes and interfaces

    def add_blackhole_route(self, prefix: str) -> None:
        self._configure(f"ip route {prefix} blackhole")
        self._exec(FRR_SAVE_COMMAND)

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
        seen = self._exec(
            f"ip link show {shlex.quote(port)} >/dev/null 2>&1 && echo present"
        )
        if present != ("present" in seen):
            action = "appear" if present else "disappear"
            raise RuntimeError(f"SONiC port {port} on {self.node} did not {action}")

    def reattach_interface(self, interface: str) -> None:
        # vslib opens an AF_PACKET socket on the container veth's ifindex when
        # it creates the HOSTIF. Moving that veth out of the namespace leaves
        # the socket with Iface=-1; the TAP stays up, so the port looks up
        # while frames no longer cross. Recreating the CONFIG_DB PORT makes
        # orchagent remove and create the HOSTIF, which binds a new socket to
        # the returned veth. The veth itself is not a TAP and is left in place.
        port = self._port(interface)
        try:
            kernel = interface if interface.startswith("eth") else container_iface(port)
        except ValueError:
            kernel = port
        if "present" not in self._exec(
            f"ip link show {shlex.quote(kernel)} >/dev/null 2>&1 && echo present"
        ):
            raise RuntimeError(
                f"SONiC kernel interface {kernel} on {self.node} is missing; "
                "cannot rebind the virtual-switch port"
            )
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
        self.__dict__.setdefault("_changed_mtu", set()).add(self._port(interface))
        self._exec(f"redis-cli -n 4 hset 'PORT|{self._port(interface)}' mtu {int(mtu)}")

    def interface_mtu(self, interface: str) -> int | None:
        port = self._port(interface)
        configured = self._exec(f"redis-cli -n 4 hget 'PORT|{port}' mtu").strip()
        kernel = self._exec(f"cat /sys/class/net/{port}/mtu 2>/dev/null").strip()
        # Both CONFIG_DB and the kernel port must carry it.
        return int(kernel) if configured.isdigit() and configured == kernel else None


FAULT_OPS = SonicFaultOps
