"""Shared FRR routing API for Kathara and Containerlab labs."""

from __future__ import annotations

import re

from nika.service.lab.protocols import SupportsExec


class FRRAPIMixin:
    """FRR routing daemon operations via ``exec_cmd``."""

    def frr_show_route(self: SupportsExec, device_name: str) -> str:
        return self.exec_cmd(device_name, "vtysh -c 'show ip route'")

    def frr_exec(self: SupportsExec, device_name: str, command: str) -> str:
        return self.exec_cmd(device_name, f"vtysh -c '{command}'")

    def frr_show_running_config(self: SupportsExec, device_name: str) -> str:
        return self.exec_cmd(device_name, "vtysh -c 'show running-config'")

    def frr_get_ospf_conf(self: SupportsExec, device_name: str) -> str:
        return self.exec_cmd(device_name, "vtysh -c 'show ip ospf'")

    def frr_get_ospf_neighbors(self: SupportsExec, device_name: str) -> str:
        return self.exec_cmd(device_name, "vtysh -c 'show ip ospf neighbor'")

    def frr_get_ospf_routes(self: SupportsExec, device_name: str) -> str:
        return self.exec_cmd(device_name, "vtysh -c 'show ip route ospf'")

    def frr_get_ospf_interfaces(self: SupportsExec, device_name: str) -> str:
        return self.exec_cmd(device_name, "vtysh -c 'show ip ospf interface'")

    def frr_get_bgp_conf(self: SupportsExec, device_name: str) -> str:
        return self.exec_cmd(device_name, "vtysh -c 'show ip bgp'")

    def frr_conf(self: SupportsExec, device_name: str, conf_commands: list[str]) -> str:
        command = 'vtysh -c "conf t"'
        for cmd in conf_commands:
            command += f' -c "{cmd}"'
        command += ' -c "end" -c "write"'
        return self.exec_cmd(device_name, command)

    def frr_add_route(
        self: SupportsExec, device_name: str, route: str, next_hop: str
    ) -> str:
        command = (
            f'vtysh -c "conf t" -c "ip route {route} {next_hop}" -c "end" -c "write"'
        )
        return self.exec_cmd(device_name, command)

    def frr_del_route(
        self: SupportsExec, device_name: str, route: str, next_hop: str
    ) -> str:
        command = (
            f'vtysh -c "conf t" -c "no ip route {route} {next_hop}" -c "end" -c "write"'
        )
        return self.exec_cmd(device_name, command)

    def frr_add_bgp_advertisement(
        self: SupportsExec, device_name: str, network: str, as_path: str
    ) -> str:
        command = (
            f'vtysh -c "conf t" -c "router bgp {as_path}" -c "network {network}" '
            f'-c "end" -c "write"'
        )
        return self.exec_cmd(device_name, command)

    def frr_del_bgp_advertisement(
        self: SupportsExec, device_name: str, network: str, as_path: str
    ) -> str:
        command = (
            f'vtysh -c "conf t" -c "router bgp {as_path}" -c "no network {network}" '
            f'-c "end" -c "write"'
        )
        return self.exec_cmd(device_name, command)

    def frr_get_bgp_asn_number(self: SupportsExec, node: str) -> int:
        summary = self.exec_cmd(
            node, "vtysh -c 'show bgp summary' 2>/dev/null || true"
        ).strip()
        match = re.search(r"local AS number\s+(\d+)", summary)
        if match:
            return int(match.group(1))

        running_config = self.exec_cmd(
            node,
            "vtysh -c 'show running-config' 2>/dev/null | grep -E '^router bgp ' | awk '{print $3}' | head -n1",
        ).strip()
        if running_config.isdigit():
            return int(running_config)

        raise ValueError(
            f"Could not determine BGP ASN for {node!r}. "
            f"summary={summary!r}, running_config_asn={running_config!r}"
        )

    def frr_get_bgp_summary(self: SupportsExec, router_name: str) -> str:
        return self.exec_cmd(router_name, "vtysh -c 'show bgp summary'")

    def frr_get_bgp_routes(
        self: SupportsExec, router_name: str, prefix: str | None = None
    ) -> str:
        if prefix:
            return self.exec_cmd(router_name, f"vtysh -c 'show ip bgp {prefix}'")
        return self.exec_cmd(router_name, "vtysh -c 'show ip bgp'")

    def frr_get_bgp_neighbor_stats(
        self: SupportsExec, router_name: str, neighbor: str | None = None
    ) -> str:
        if neighbor:
            return self.exec_cmd(
                router_name, f"vtysh -c 'show bgp neighbors {neighbor}'"
            )
        return self.exec_cmd(router_name, "vtysh -c 'show bgp neighbors'")

    def frr_get_routing_state(
        self: SupportsExec,
        device: str,
        *,
        neighbor: str | None = None,
        prefix: str | None = None,
    ) -> str:
        """Aggregate BGP telemetry for agents and diagnostics.

        Returns summary, neighbor detail (session state, received/accepted
        prefixes, configured maximum-prefix, last reset), and RIB output
        (optionally filtered to ``prefix``).
        """
        chunks: list[str] = [
            f"=== BGP summary ({device}) ===",
            self.frr_get_bgp_summary(device),
            "",
        ]
        if neighbor:
            chunks.append(f"=== BGP neighbor {neighbor} ({device}) ===")
            chunks.append(self.frr_get_bgp_neighbor_stats(device, neighbor=neighbor))
        else:
            chunks.append(f"=== BGP neighbors ({device}) ===")
            chunks.append(self.frr_get_bgp_neighbor_stats(device))
        chunks.append("")
        if prefix:
            chunks.append(f"=== BGP RIB {prefix} ({device}) ===")
            chunks.append(self.frr_get_bgp_routes(device, prefix=prefix))
        else:
            chunks.append(f"=== BGP RIB ({device}) ===")
            chunks.append(self.frr_get_bgp_routes(device))
        running = self.exec_cmd(
            device,
            "vtysh -c 'show running-config' 2>/dev/null | "
            "grep -E 'maximum-prefix|neighbor .* remote-as' || true",
        )
        chunks.extend(
            [
                "",
                f"=== BGP configured neighbors / maximum-prefix ({device}) ===",
                running or "(none)",
            ]
        )
        return "\n".join(chunks)

    def frr_get_rpki_status(
        self: SupportsExec, device: str, prefix: str | None = None
    ) -> str:
        """Return RTR connection / validation summary, optionally for one prefix."""
        chunks: list[str] = []
        chunks.append(
            self.exec_cmd(
                device, "vtysh -c 'show rpki cache-connection' 2>/dev/null || true"
            )
        )
        chunks.append(
            self.exec_cmd(
                device, "vtysh -c 'show rpki cache-server' 2>/dev/null || true"
            )
        )
        if prefix:
            chunks.append(
                self.exec_cmd(
                    device,
                    f"vtysh -c 'show rpki prefix-table {prefix}' 2>/dev/null || "
                    f"vtysh -c 'show rpki prefix {prefix}' 2>/dev/null || true",
                )
            )
            chunks.append(
                self.exec_cmd(
                    device,
                    f"vtysh -c 'show bgp ipv4 unicast {prefix}' 2>/dev/null || true",
                )
            )
        else:
            chunks.append(
                self.exec_cmd(
                    device, "vtysh -c 'show rpki prefix-table' 2>/dev/null || true"
                )
            )
        return "\n".join(chunk for chunk in chunks if chunk is not None)
