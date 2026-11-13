"""Controller-held snapshots for failure recovery."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import re
import shlex

from nika.problems.support.dhcp import restart_dhcp_server


def checked_exec(runtime, node: str, command: str) -> str:
    """Run a command and reject nonzero exits reported as ordinary output."""
    marker = "NIKA_RECOVERY_COMMAND_OK"
    output = runtime.exec(node, f"({command}) && printf '\\n{marker}\\n'")
    lines = output.rstrip().splitlines()
    if marker not in lines:
        raise RuntimeError(f"Recovery command failed on {node}")
    return "\n".join(line for line in lines if line != marker).removesuffix("\n")


@dataclass(frozen=True)
class FileSnapshot:
    """File contents held outside the network environment."""

    path: str
    content: str | None

    @classmethod
    def capture(cls, runtime, node: str, path: str) -> FileSnapshot:
        quoted = shlex.quote(path)
        output = checked_exec(
            runtime,
            node,
            f"if test -e {quoted}; then printf 'PRESENT\\n'; base64 {quoted}; "
            "else printf 'ABSENT\\n'; fi",
        )
        status, _, encoded = output.partition("\n")
        if status == "ABSENT":
            return cls(path, None)
        if status != "PRESENT":
            raise RuntimeError(f"Cannot read {path} on {node}")
        return cls(path, base64.b64decode(encoded).decode())

    def restore(self, runtime, node: str) -> None:
        quoted = shlex.quote(self.path)
        if self.content is None:
            checked_exec(runtime, node, f"rm -f -- {quoted}")
        else:
            encoded = base64.b64encode(self.content.encode()).decode()
            checked_exec(
                runtime,
                node,
                f"printf %s {shlex.quote(encoded)} | base64 -d > {quoted}",
            )
        if not self.matches(runtime, node):
            raise RuntimeError(f"File recovery failed for {self.path} on {node}")

    def matches(self, runtime, node: str) -> bool:
        return self.capture(runtime, node, self.path).content == self.content


def route_lines(output: str) -> list[str]:
    """Join multipath next hops and remove read-only route state flags."""
    routes: list[str] = []
    flags = {"linkdown", "dead", "offload", "trap", "rt_offload"}
    for line in output.splitlines():
        words = [word for word in line.split() if word not in flags]
        if not words:
            continue
        line = " ".join(words)
        if words[0] == "nexthop" and routes:
            routes[-1] += f" {line}"
        else:
            routes.append(line)
    return routes


@dataclass(frozen=True)
class IPv4Snapshot:
    """Addresses, static routes, and permanent neighbors affected by address deletion."""

    interface: str
    addresses: tuple[str, ...]
    routes: tuple[str, ...]
    neighbors: tuple[str, ...]

    @classmethod
    def capture(cls, runtime, node: str, interface: str = "eth0") -> IPv4Snapshot:
        dev = shlex.quote(interface)
        addresses = checked_exec(
            runtime, node, f"ip -4 -o addr show dev {dev} scope global"
        )
        routes = checked_exec(runtime, node, f"ip -4 route show table all dev {dev}")
        neighbors = checked_exec(
            runtime, node, f"ip -4 neigh show dev {dev} nud permanent"
        )
        return cls(
            interface,
            tuple(re.findall(r"\binet (\S+)", addresses)),
            tuple(
                route for route in route_lines(routes) if "proto kernel" not in route
            ),
            tuple(neighbors.splitlines()),
        )

    def restore(
        self,
        runtime,
        node: str,
        *,
        injected_ip: str | None = None,
        injected_gateway: str | None = None,
    ) -> None:
        dev = shlex.quote(self.interface)
        if injected_ip and injected_ip not in self.addresses:
            current = checked_exec(
                runtime, node, f"ip -4 -o addr show dev {dev} scope global"
            )
            if injected_ip in re.findall(r"\binet (\S+)", current):
                checked_exec(
                    runtime, node, f"ip addr del {shlex.quote(injected_ip)} dev {dev}"
                )
        for address in self.addresses:
            checked_exec(
                runtime, node, f"ip addr replace {shlex.quote(address)} dev {dev}"
            )
        has_main_default = any(
            route.startswith("default ")
            and (" table " not in route or " table main " in route + " ")
            for route in self.routes
        )
        if injected_gateway and not has_main_default:
            routes = checked_exec(runtime, node, f"ip -4 route show default dev {dev}")
            for route in route_lines(routes):
                if f"via {injected_gateway} " in route + " ":
                    checked_exec(runtime, node, "ip -4 route del " + route)
        for route in sorted(self.routes, key=lambda row: " via " in row):
            checked_exec(runtime, node, "ip -4 route replace " + route)
        for neighbor in self.neighbors:
            match = re.match(r"(\S+) (?:dev \S+ )?lladdr (\S+)", neighbor)
            if match:
                checked_exec(
                    runtime,
                    node,
                    f"ip neigh replace {match[1]} lladdr {match[2]} dev {dev} nud permanent",
                )
        restored = self.capture(runtime, node, self.interface)
        if (
            not set(self.addresses).issubset(restored.addresses)
            or not set(self.routes).issubset(restored.routes)
            or not set(self.neighbors).issubset(restored.neighbors)
        ):
            raise RuntimeError(f"IPv4 recovery failed on {node}:{self.interface}")


@dataclass(frozen=True)
class DHCPRecovery:
    """DHCP server configuration and client state before lease renewal."""

    config: FileSnapshot
    was_running: bool
    clients: tuple[tuple[str, IPv4Snapshot, FileSnapshot], ...]

    @classmethod
    def capture(cls, runtime, server: str) -> DHCPRecovery:
        return cls(
            FileSnapshot.capture(runtime, server, "/etc/dhcp/dhcpd.conf"),
            not runtime.process_not_running(server, "dhcpd"),
            tuple(
                (
                    client,
                    IPv4Snapshot.capture(runtime, client),
                    FileSnapshot.capture(runtime, client, "/etc/resolv.conf"),
                )
                for client in runtime.list_dhcp_client_nodes()
            ),
        )

    def restore(
        self, runtime, server: str, *, service: str = "isc-dhcp-server"
    ) -> bool:
        self.config.restore(runtime, server)
        if self.was_running:
            restart_dhcp_server(runtime.exec, server, service=service)
        else:
            runtime.systemctl(server, service, "stop")
        running = not runtime.process_not_running(server, "dhcpd")
        if running != self.was_running:
            return False
        if self.was_running:
            runtime.renew_dhcp_leases([client for client, _, _ in self.clients])
        for client, ipv4, resolver in self.clients:
            restored = IPv4Snapshot.capture(runtime, client)
            if not set(ipv4.addresses).issubset(restored.addresses):
                return False
            if not set(ipv4.routes).issubset(restored.routes):
                return False
            current = FileSnapshot.capture(runtime, client, resolver.path)

            def nameservers(value):
                return re.findall(r"^nameserver\s+(\S+)", value or "", re.M)

            if nameservers(current.content) != nameservers(resolver.content):
                return False
        return True
