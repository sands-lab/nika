"""MikroTik RouterOS provider for failure operations on native routers.

Each operation applies a failure's root cause with RouterOS commands through
the internal management hop. RouterOS does not filter firewall rows by
interface or address in ``find``/``where`` unless values are quoted, so the
provider quotes typed values or selects rows after reading them.
"""

from __future__ import annotations

import re

from nika.net_env.utils.routeros.common import routeros_port
from nika.net_env.utils.routeros.parse import (
    routeros_count,
    routeros_established_bgp_peers,
    routeros_terse_rows,
)
from nika.problems.support.router_ops import RouterFaultOps, SourceNat, TunnelPeer
from nika.service.lab.routeros_api import CLI_COMMAND

_BGP_PORT_RULES = tuple(
    (chain, port)
    for chain in ("input", "forward", "output")
    for port in ("dst-port", "src-port")
)
_FILTER_CHAINS = ("input", "forward", "output")
_DURATION = re.compile(r"(\d+)(ms|w|d|h|m|s)")
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}


class RouterOsFaultOps(RouterFaultOps):
    profile = "routeros"

    # CLI access

    def _cli(self, command: str) -> str:
        return self.runtime.exec(
            self.node, CLI_COMMAND.format(command=command), timeout=30
        )

    def _rows(self, command: str) -> list[dict[str, str]]:
        return routeros_terse_rows(self._cli(command))

    def _port(self, interface: str) -> str:
        """Map a container interface to its RouterOS port; keep other names."""
        if re.fullmatch(r"eth\d+", interface):
            net_env = getattr(self.runtime, "_net_env", None)
            return routeros_port(net_env, self.node, interface)
        return interface

    def _reset_connections(self, names: list[str] | None = None) -> None:
        # Without a pause RouterOS keeps the sessions it would reset.
        selector = (
            "[find]"
            if names is None
            else "[find " + " or ".join(f'name="{n}"' for n in names) + "]"
        )
        self._cli(
            f"/routing bgp connection disable {selector}; :delay 2s; "
            f"/routing bgp connection enable {selector}"
        )

    # BGP

    def bgp_asn(self) -> int:
        values = {
            row.get("as") for row in self._rows("/routing/bgp/instance/print terse")
        }
        if len(values) != 1 or not str(next(iter(values))).isdigit():
            raise ValueError(f"No single BGP AS on RouterOS router {self.node!r}")
        return int(next(iter(values)))

    def set_bgp_asn(self, asn: int) -> None:
        self._cli(f"/routing bgp instance set [find] as={int(asn)}")

    def _origin_lists(self) -> set[str]:
        rows = self._rows("/routing/bgp/connection/print terse")
        return {row["output.network"] for row in rows if row.get("output.network")}

    def _exported_vrfs(self) -> dict[str, list[str]]:
        """VRFs whose connected LANs BGP VPN exports, with those LANs."""
        exported = {}
        for row in self._rows("/routing/bgp/vpn/print terse"):
            if "connected" not in row.get("export.redistribute", ""):
                continue
            vrf = row["vrf"]
            exported[vrf] = [
                route["dst-address"]
                for route in self._rows(
                    f"/routing/route/print terse where routing-table={vrf} "
                    "and connect and afi=ip"
                )
            ]
        return exported

    def bgp_networks(self) -> list[str]:
        networks = [
            row["address"]
            for name in sorted(self._origin_lists())
            for row in self._rows(
                f'/ip/firewall/address-list/print terse where list="{name}"'
            )
        ]
        networks += [p for lans in self._exported_vrfs().values() for p in lans]
        return sorted(set(networks))

    def withdraw_bgp_network(self, prefix: str) -> None:
        for name in self._origin_lists():
            self._cli(
                "/ip firewall address-list remove "
                f'[find list="{name}" address="{prefix}"]'
            )
        for vrf, lans in self._exported_vrfs().items():
            if prefix in lans:
                # A business VRF exports exactly its connected LAN.
                self._cli(
                    f'/routing bgp vpn unset [find vrf="{vrf}"] export.redistribute'
                )

    def bgp_established_peers(self) -> set[str]:
        return routeros_established_bgp_peers(
            self._cli("/routing/bgp/session/print terse where established=yes")
        )

    def reset_bgp_session(self, peer: str) -> None:
        names = [
            row["name"]
            for row in self._rows("/routing/bgp/connection/print terse")
            if row.get("remote.address", "").split("@")[0] == peer
        ]
        if names:
            self._reset_connections(names)

    def remote_bgp_prefixes(self) -> list[str]:
        def prefixes(query: str) -> set[str]:
            return {
                row["dst-address"] for row in self._rows(query) if "dst-address" in row
            }

        # A VRF router also learns routes for LANs it serves in another table.
        return sorted(
            prefixes("/routing/route/print terse where bgp and active and afi=ip")
            - prefixes("/routing/route/print terse where connect and afi=ip")
        )

    def originate_bgp_prefix(self, prefix: str) -> None:
        from ipaddress import ip_network

        from nika.net_env.utils.kathara.frr_origin import BGP_ORIGIN_IFACE

        net = ip_network(prefix, strict=False)
        self._cli(f"/interface bridge add name={BGP_ORIGIN_IFACE} protocol-mode=none")
        # Originate in the BGP instance's table: its VRF, or main.
        vrf = next(
            (
                row["vrf"]
                for row in self._rows("/routing/bgp/instance/print terse")
                if row.get("vrf")
            ),
            None,
        )
        if vrf:
            members = self._cli(f':put [/ip vrf get [find name="{vrf}"] interfaces]')
            self._cli(
                f'/ip vrf set [find name="{vrf}"] '
                f"interfaces={members.strip().replace(';', ',')},{BGP_ORIGIN_IFACE}"
            )
        # The router answers for the whole prefix, as FRR's local route does.
        self._cli(
            f"/ip address add address={next(net.hosts())}/{net.prefixlen} "
            f"interface={BGP_ORIGIN_IFACE}"
        )
        lists = self._origin_lists()
        name = next(iter(lists)) if len(lists) == 1 else "BGP-NETWORKS"
        self._cli(f"/ip firewall address-list add list={name} address={net}")
        if not lists:
            self._cli(f"/routing bgp connection set [find] output.network={name}")

    def bgp_advertised_to(self, prefix: str) -> list[str]:
        return sorted(
            row["peer"]
            for row in self._rows("/routing/bgp/advertisements/print terse")
            if row.get("dst") == prefix and "peer" in row
        )

    # Packet filters

    # Running configuration

    def save_config(self) -> str:
        return self._cli("/export terse")

    def restore_config(self, saved: str) -> None:
        from nika.net_env.utils.routeros.common import routeros_restore_script

        script = routeros_restore_script(saved, self._cli("/export terse"))
        if script:
            self._cli(script)

    # OSPF

    def ospf_interface_areas(self) -> list[str]:
        areas = {
            row["name"]: row["area-id"]
            for row in self._rows("/routing/ospf/area/print terse")
            if "name" in row and "area-id" in row
        }
        return [
            areas.get(row.get("area", ""), row.get("area", ""))
            for row in self._rows("/routing/ospf/interface-template/print terse")
        ]

    def renumber_ospf_area(self, area: str, new_area: str) -> None:
        # RouterOS keeps the established neighbors of a reconfigured area, so
        # the instance restarts to run the new area ID, as a restarted FRR
        # daemon runs an edited configuration.
        self._cli(
            f'/routing ospf area set [find where area-id="{area}"] '
            f'area-id="{new_area}"; /routing ospf instance disable [find]; '
            ":delay 2s; /routing ospf instance enable [find]"
        )

    def _filter_rules(self) -> list[dict[str, str]]:
        return self._rows("/ip/firewall/filter/print terse")

    def block_bgp_transport(self) -> None:
        for chain, port in _BGP_PORT_RULES:
            self._cli(
                f"/ip firewall filter add chain={chain} protocol=tcp {port}=179 action=drop"
            )
        self._reset_connections()

    def bgp_transport_blocked(self) -> bool:
        rules = self._filter_rules()
        return all(
            any(
                row.get("chain") == chain
                and row.get("protocol") == "tcp"
                and row.get(port) == "179"
                and row.get("action") == "drop"
                for row in rules
            )
            for chain, port in _BGP_PORT_RULES
        )

    def block_icmp_frag_needed(self) -> None:
        for chain in _FILTER_CHAINS:
            self._cli(
                f"/ip firewall filter add chain={chain} protocol=icmp "
                "icmp-options=3:4 action=drop"
            )

    def icmp_frag_needed_blocked(self) -> bool:
        chains = {
            row.get("chain")
            for row in self._filter_rules()
            if row.get("protocol") == "icmp"
            and row.get("icmp-options") == "3:4"
            and row.get("action") == "drop"
        }
        return set(_FILTER_CHAINS) <= chains

    # Routes and interfaces

    def add_blackhole_route(self, prefix: str) -> None:
        tables = {
            row.get("routing-table", "main")
            for row in self._rows(
                f"/routing/route/print terse where dst-address={prefix} and active"
            )
        }
        for table in sorted(tables | {"main"}):
            self._cli(
                f"/ip route add dst-address={prefix} blackhole routing-table={table}"
            )

    def blackhole_routes(self) -> list[str]:
        return sorted(
            {
                row["dst-address"]
                for row in self._rows("/ip/route/print terse where blackhole")
                if "dst-address" in row
            }
        )

    def route_interface(self, prefix: str) -> str | None:
        for row in self._rows(
            f"/routing/route/print terse where dst-address={prefix} and active"
        ):
            gateway = row.get("immediate-gw", "")
            if "%" in gateway:
                return gateway.split("%", 1)[1]
        return None

    def interface_up(self, interface: str) -> bool:
        return (
            routeros_count(
                self._cli(
                    f"/interface/print count-only where name={self._port(interface)} "
                    "and running"
                )
            )
            > 0
        )

    def set_interface_mtu(self, interface: str, mtu: int) -> None:
        # The container link only carries the VM's frames; the router port
        # must know the MTU to fragment or send ICMP Fragmentation Needed.
        self.runtime.exec(self.node, f"ip link set dev {interface} mtu {int(mtu)}")
        self._cli(
            f"/interface ethernet set [find name={self._port(interface)}] mtu={int(mtu)}"
        )

    def interface_mtu(self, interface: str) -> int | None:
        rows = self._rows(f"/interface/print terse where name={self._port(interface)}")
        value = rows[0].get("mtu") if rows else None
        native = int(value) if value and value.isdigit() else None
        # Both the router port and its container link must carry it.
        link = re.search(
            r"\bmtu\s+(\d+)\b",
            self.runtime.exec(self.node, f"ip -o link show dev {interface}"),
        )
        return native if link and native == int(link[1]) else None

    def interface_addresses(self, interface: str) -> list[str]:
        return [
            row["address"]
            for row in self._rows(
                f"/ip/address/print terse where interface={self._port(interface)}"
            )
            if "address" in row
        ]

    def remove_interface_address(self, interface: str, address: str) -> None:
        self._cli(f'/ip address remove [find address="{address}"]')

    # Tunnels, QoS marking and NAT

    def tunnel_peer(self, interface: str) -> TunnelPeer:
        rows = self._rows(
            f"/interface/wireguard/peers/print terse where interface={interface}"
        )
        if not rows:
            raise ValueError(f"No WireGuard peer on {self.node}:{interface}")
        peer = rows[0]
        age = None
        if peer.get("last-handshake"):
            age = sum(
                float(amount) * _UNITS[unit]
                for amount, unit in _DURATION.findall(peer["last-handshake"])
            )
        allowed = tuple(a for a in peer.get("allowed-address", "").split(",") if a)
        return TunnelPeer(peer.get("public-key", ""), allowed, age)

    def set_tunnel_peer_public_key(self, interface: str, key: str) -> None:
        # Changing the key replaces the peer's session, as wg syncconf does.
        self._cli(
            f'/interface wireguard peers set [find interface={interface}] public-key="{key}"'
        )

    def set_tunnel_peer_allowed_addresses(
        self, interface: str, prefixes: list[str]
    ) -> None:
        self._cli(
            f"/interface wireguard peers set [find interface={interface}] "
            f"allowed-address={','.join(prefixes)}"
        )

    def _dscp_remarks(self, interface: str, from_dscp: int, to_dscp: int):
        return [
            row
            for row in self._rows(
                "/ip/firewall/mangle/print terse where action=change-dscp"
            )
            if row.get("out-interface") == interface
            and row.get("dscp") == str(from_dscp)
            and row.get("new-dscp") == str(to_dscp)
        ]

    def add_dscp_remark(
        self, interface: str, source_prefix: str, from_dscp: int, to_dscp: int
    ) -> None:
        # Postrouting mangle runs before the queue-tree marks, as nft's mangle
        # hook runs before tc classification on Linux.
        self._cli(
            f"/ip firewall mangle add chain=postrouting src-address={source_prefix} "
            f"out-interface={interface} dscp={from_dscp} action=change-dscp "
            f"new-dscp={to_dscp} passthrough=yes "
            "place-before=[:pick [/ip firewall mangle find chain=postrouting] 0]"
        )

    def dscp_remark_present(self, interface: str, from_dscp: int, to_dscp: int) -> bool:
        return bool(self._dscp_remarks(interface, from_dscp, to_dscp))

    def remove_dscp_remark(self, interface: str, from_dscp: int, to_dscp: int) -> None:
        for row in self._dscp_remarks(interface, from_dscp, to_dscp):
            self._cli(
                "/ip firewall mangle remove [find action=change-dscp "
                f'src-address="{row.get("src-address")}"]'
            )

    def vrf_routes(self, vrf: str) -> list[str]:
        return [
            row["dst-address"]
            for row in self._rows(
                f"/routing/route/print terse where routing-table={vrf} and afi=ip"
            )
            if "dst-address" in row
        ]

    def add_source_nat(
        self,
        source_prefix: str,
        to_address: str,
        *,
        protocol: str | None = None,
        to_ports: str | None = None,
    ) -> None:
        command = "/ip firewall nat add chain=srcnat"
        if protocol:
            command += f" protocol={protocol}"
        command += (
            f" src-address={source_prefix} action=src-nat to-addresses={to_address}"
        )
        if to_ports:
            command += f" to-ports={to_ports}"
        self._cli(
            command + " place-before=[:pick [/ip firewall nat find chain=srcnat] 0]"
        )

    def source_nat_rules(self) -> list[SourceNat]:
        return [
            SourceNat(
                row.get("src-address", ""),
                row.get("to-addresses", ""),
                row.get("to-ports"),
                row.get("protocol"),
            )
            for row in self._rows("/ip/firewall/nat/print terse where chain=srcnat")
        ]

    def clear_nat_mappings(self, address: str) -> None:
        self._cli(
            "/ip firewall connection remove [find protocol=tcp and "
            f'reply-dst-address~"^{address}:"]; '
            "/ip firewall connection remove [find protocol=tcp and "
            f'dst-address~"^{address}:"]'
        )

    def nat_mapping_count(self, address: str) -> int:
        return routeros_count(
            self._cli(
                "/ip/firewall/connection/print count-only where protocol=tcp and "
                f'reply-dst-address~"^{address}:"'
            )
        )
