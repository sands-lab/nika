"""MikroTik RouterOS provider for failure operations on native routers.

Each operation applies a failure's root cause with RouterOS commands through
the internal management hop. RouterOS does not filter firewall rows by
interface or address in ``find``/``where`` unless values are quoted, so the
provider quotes typed values or selects rows after reading them.
"""

from __future__ import annotations

import ipaddress
import re

from nika.net_env.utils.routeros.common import routeros_port
from nika.net_env.utils.routeros.parse import (
    _CLI_ERROR,
    routeros_bgp_received_notification,
    routeros_count,
    routeros_established_bgp_peers,
    routeros_filter_rules,
    routeros_terse_rows,
)
from nika.problems.support.router_ops import (
    OspfNeighbor,
    RouterFaultOps,
    SourceNat,
    TunnelPeer,
)
from nika.service.lab.routeros_api import CLI_COMMAND

_BGP_PORT_RULES = tuple(
    (chain, port)
    for chain in ("input", "forward", "output")
    for port in ("dst-port", "src-port")
)
_FILTER_CHAINS = ("input", "forward", "output")
# Route filter chains that BGP sessions name but no rule defines.
_UNDEFINED_IN_CHAIN = "bgp-in"
_UNDEFINED_OUT_CHAIN = "bgp-out"
_DURATION = re.compile(r"(\d+)(ms|w|d|h|m|s)")
_UNITS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}


def _address_match(field: str, address: str) -> str:
    """Match a connection address field, with or without its port.

    RouterOS 7.21 prints the port in a separate field; earlier releases append
    it to the address (``198.18.1.10:40840``).
    """
    return f'{field}="{address}" or {field}~"^{address}:"'


class RouterOsFaultOps(RouterFaultOps):
    profile = "routeros"

    # CLI access

    def _cli(self, command: str) -> str:
        output = self.runtime.exec(
            self.node, CLI_COMMAND.format(command=command), timeout=30
        )
        if output.startswith("[TIMEOUT]"):
            raise RuntimeError(f"RouterOS command timed out on {self.node}: {command}")
        if _CLI_ERROR.search(output):
            raise RuntimeError(
                f"RouterOS command failed on {self.node}: {output.strip()}"
            )
        return output

    def _rows(self, command: str) -> list[dict[str, str]]:
        return routeros_terse_rows(self._cli(command))

    def _port(self, interface: str) -> str:
        """Map a container interface to its RouterOS port; keep other names."""
        if re.fullmatch(r"eth\d+", interface):
            # The environment decides the layout: a converted router's ``ethN``
            # is ``ether{N+2}``, an ISP router's is ``ether{N+1}``. Runtimes
            # without a Kathara environment only run reserved-layout routers.
            net_env = getattr(self.runtime, "_net_env", None)
            return routeros_port(net_env, self.node, interface)
        return interface

    def _reset_connections(
        self, names: list[str] | None = None, *, before_enable: str = ""
    ) -> None:
        # Without a pause RouterOS keeps the sessions it would reset.
        selector = (
            "[find]"
            if names is None
            else "[find " + " or ".join(f'name="{n}"' for n in names) + "]"
        )
        self._cli(
            f"/routing bgp connection disable {selector}; :delay 2s; "
            + (f"{before_enable}; " if before_enable else "")
            + f"/routing bgp connection enable {selector}"
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
            if row.get("remote.address", "").split("@")[0].split("/")[0] == peer
        ]
        if names:
            self._cli(
                f'/routing bgp session clear [find where remote.address="{peer}"] flag=limit-exceeded'
            )
            self._reset_connections(names)

    def _bgp_session_output(self, peer: str) -> str:
        return self._cli(
            f'/routing/bgp/session/print terse where remote.address="{ipaddress.IPv4Address(peer)}"'
        )

    def bgp_received_prefix_count(self, peer: str) -> int:
        rows = routeros_terse_rows(self._bgp_session_output(peer))
        if len(rows) != 1 or not rows[0].get("prefix-count", "").isdigit():
            raise ValueError(
                f"RouterOS omitted received prefix count for {peer}: {rows}"
            )
        return int(rows[0]["prefix-count"])

    def _bgp_connection(self, peer: str) -> dict[str, str]:
        peer = str(ipaddress.IPv4Address(peer))
        connections = [
            row
            for row in self._rows("/routing/bgp/connection/print terse")
            if row.get("remote.address", "").split("@")[0].split("/")[0] == peer
        ]
        if len(connections) != 1:
            raise ValueError(f"No single BGP connection to {peer} on {self.node!r}")
        return connections[0]

    def set_bgp_ipv4_unicast(self, peer: str, enabled: bool) -> None:
        connection = self._bgp_connection(peer)
        families = set(filter(None, connection.get("afi", "ip").split(",")))
        if enabled:
            families.add("ip")
        else:
            families.discard("ip")
        # Keep a non-IPv4 family when IPv4 was the only configured family.
        afi = ",".join(sorted(families or {"ipv6"}))
        self._reset_connections(
            [connection["name"]],
            before_enable=(
                f'/routing bgp connection set [find name="{connection["name"]}"] '
                f"afi={afi}"
            ),
        )

    def restore_bgp_ipv4_unicast(self, peer: str, configuration: str) -> None:
        connection = self._bgp_connection(peer)
        self._reset_connections(
            [connection["name"]],
            before_enable=(
                f'/routing bgp connection set [find name="{connection["name"]}"] '
                f"afi={configuration}"
            ),
        )

    def bgp_ipv4_unicast_status(self, peer: str) -> dict:
        peer = str(ipaddress.IPv4Address(peer))
        connection = self._bgp_connection(peer)
        sessions = [
            row
            for row in self._rows(
                "/routing/bgp/session/print terse where established=yes"
            )
            if row.get("remote.address", "").split("@")[0] == peer
        ]
        if len(sessions) > 1:
            raise ValueError(f"Ambiguous BGP session to {peer} on {self.node!r}")
        session = sessions[0] if len(sessions) == 1 else {}
        count = session.get("prefix-count", "")
        if session and not count.isdigit():
            raise ValueError(f"Unreadable BGP prefix count for {peer} on {self.node!r}")
        configured = set(connection.get("afi", "ip").split(","))
        local = set(session.get("local.afi", "").split(","))
        remote = set(session.get("remote.afi", "").split(","))
        return {
            "configuration": connection.get("afi", "ip"),
            "configured": "ip" in configured,
            "negotiated": "ip" in local & remote,
            "state": "Established" if session else "NotEstablished",
            "received_prefix_count": int(count) if session else 0,
        }

    def bgp_prefix_limit(self, peer: str) -> int | None:
        rows = self._rows(
            f'/routing/bgp/connection/print terse where remote.address="{ipaddress.IPv4Address(peer)}"'
        )
        if len(rows) != 1:
            raise ValueError(f"No single BGP connection to {peer}")
        value = rows[0].get("input.limit-process-routes-ipv4")
        return int(value) if value else None

    def set_bgp_prefix_limit(self, peer: str, limit: int) -> None:
        self._cli(
            f'/routing bgp connection set [find where remote.address="{ipaddress.IPv4Address(peer)}"] input.limit-process-routes-ipv4={int(limit)}'
        )

    def bgp_prefix_limit_exceeded(self, peer: str) -> bool:
        output = self._bgp_session_output(peer)
        return bool(re.search(r"(?:^|\s)limit-exceeded(?:\s|$)", output))

    def bgp_last_notification(self, peer: str) -> str | None:
        return routeros_bgp_received_notification(self._bgp_session_output(peer))

    def permit_bgp_prefixes(self, prefix: str, length: int) -> None:
        chains = {
            row[key]
            for row in self._rows("/routing/bgp/connection/print terse")
            for key in ("input.filter", "output.filter-chain")
            if row.get(key)
        }
        commands = [
            f'/routing filter rule add chain="{chain}" '
            f'rule="if (dst in {ipaddress.ip_network(prefix)} && dst-len == {int(length)}) {{ accept; }}" '
            f'place-before=[:pick [/routing filter rule find where chain="{chain}"] 0]'
            for chain in sorted(chains)
        ]
        if not commands:
            raise ValueError(f"No bound BGP filters on {self.node}")
        self._cli("; ".join(commands))

    def advertise_bgp_prefixes(self, prefixes: list[str]) -> None:
        lists = self._origin_lists()
        if not lists:
            raise ValueError(f"No BGP origin list on {self.node}")
        self._cli(
            "; ".join(
                command
                for prefix in prefixes
                for command in [
                    f"/ip route add dst-address={ipaddress.ip_network(prefix)} blackhole",
                    *(
                        f'/ip firewall address-list add list="{name}" address={ipaddress.ip_network(prefix)}'
                        for name in sorted(lists)
                    ),
                ]
            )
        )

    def has_bgp_route(self, prefix: str) -> bool:
        return bool(
            self._rows(
                f'/routing/route/print terse where bgp and afi=ip and dst-address="{ipaddress.ip_network(prefix)}"'
            )
        )

    def set_bgp_password(self, peer: str, password: str | None) -> None:
        peer = str(ipaddress.ip_address(peer))
        self._cli(
            f'/routing bgp connection set [find remote.address="{peer}"] '
            f'tcp-md5-key="{password or ""}"'
        )

    def bgp_password_configured(self, peer: str) -> bool:
        peer = str(ipaddress.ip_address(peer))
        output = self._cli(
            ":put ([:len [/routing bgp connection get "
            f'[find remote.address="{peer}"] tcp-md5-key]] > 0)'
        )
        value = output.strip()
        if value not in {"true", "false"}:
            raise ValueError(
                f"Unreadable BGP password state for {peer} on {self.node!r}"
            )
        return value == "true"

    def bgp_received_prefixes(self, peer: str) -> set[str]:
        peer = str(ipaddress.ip_address(peer))
        rows = self._rows("/routing/route/print terse where bgp and afi=ip")
        prefixes = {
            row["dst-address"]
            for row in rows
            if row.get("belongs-to", "").split("@", 1)[0] == f"bgp-IP-{peer}"
            and "dst-address" in row
        }
        return prefixes

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

    def _import_prefix_rules(self) -> dict[str, tuple[str, str, int]]:
        chains = {
            row["input.filter"]
            for row in self._rows("/routing/bgp/connection/print terse")
            if row.get("input.filter")
        }
        rules = {}
        for row in routeros_filter_rules(
            self._cli("/routing/filter/rule/print terse where !disabled")
        ):
            chain, rule = row.get("chain"), row.get("rule", "")
            if chain not in chains:
                continue
            match = re.fullmatch(
                r"if \(dst (in|==) (\d+\.\d+\.\d+\.\d+/\d+)"
                r"(?: && dst-len <= (\d+))?\) \{ ([^{}]*accept;) \}",
                rule,
            )
            if not match:
                continue
            operator, prefix, length, _action = match.groups()
            network = ipaddress.ip_network(prefix)
            if operator == "in" and length is None:
                continue
            rules[chain] = (rule, str(network), int(length or network.prefixlen))
        return rules

    def bgp_import_prefix_ranges(self) -> dict[str, tuple[str, int]]:
        return {k: (p, n) for k, (_rule, p, n) in self._import_prefix_rules().items()}

    def set_bgp_import_prefix_range(
        self, name: str, prefix: str, maximum_length: int
    ) -> None:
        rule, existing, _length = self._import_prefix_rules()[name]
        network = ipaddress.ip_network(prefix)
        if existing != str(network) or not network.prefixlen <= maximum_length <= 32:
            raise ValueError("Invalid BGP import prefix range")
        condition = (
            f"dst == {network}"
            if maximum_length == network.prefixlen
            else f"dst in {network} && dst-len <= {int(maximum_length)}"
        )
        updated = re.sub(r"(?<=if \().*?(?=\))", condition, rule, count=1)
        self._cli(
            f'/routing filter rule set [find chain="{name}" rule="{rule}"] '
            f'rule="{updated}"; '
            "/routing bgp session refresh [find]"
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
        # An export filter that admits only the router's own prefixes would
        # keep the hijack in; the misconfiguration admits it there too.
        chains = {
            row["output.filter-chain"]
            for row in self._rows("/routing/bgp/connection/print terse")
            if row.get("output.filter-chain")
        }
        for chain in sorted(chains):
            self._cli(
                f"/routing filter rule add chain={chain} "
                f'rule="if (dst == {net}) {{ accept; }}" '
                f'place-before=[:pick [/routing filter rule find chain="{chain}"] 0]'
            )

    def bgp_advertised_to(self, prefix: str) -> list[str]:
        return sorted(
            row["peer"]
            for row in self._rows("/routing/bgp/advertisements/print terse")
            if row.get("dst") == prefix and "peer" in row
        )

    def _ebgp_connections(self) -> list[dict[str, str]]:
        return [
            row
            for row in self._rows("/routing/bgp/connection/print terse")
            if row.get("local.role") == "ebgp"
        ]

    def drop_bgp_session_policy(self) -> None:
        # RouterOS rejects every route that a filter chain without rules sees,
        # so naming a chain that does not exist rejects the whole session.
        names = [row["name"] for row in self._ebgp_connections()]
        if names:
            selector = "[find " + " or ".join(f'name="{n}"' for n in names) + "]"
            self._cli(
                f"/routing bgp connection set {selector} "
                f"input.filter={_UNDEFINED_IN_CHAIN} "
                f"output.filter-chain={_UNDEFINED_OUT_CHAIN}"
            )

    def bgp_session_policy_dropped(self) -> bool:
        def defined(chain: str) -> bool:
            if not chain:
                return True  # no filter: RouterOS accepts every route
            count = self._cli(
                f'/routing/filter/rule/print count-only where chain="{chain}"'
            )
            if not count.strip().isdigit():
                raise ValueError(f"Unreadable BGP filter count on {self.node!r}")
            return int(count.strip()) > 0

        rows = self._ebgp_connections()
        return bool(rows) and all(
            not defined(row.get("input.filter", ""))
            and not defined(row.get("output.filter-chain", ""))
            for row in rows
        )

    # Running configuration

    def save_config(self) -> str:
        saved = self._cli("/export terse")
        if not saved.strip():
            raise ValueError(f"Unreadable RouterOS configuration on {self.node!r}")
        return saved

    def restore_config(self, saved: str) -> None:
        from nika.net_env.utils.routeros.common import routeros_restore_script

        script = routeros_restore_script(saved, self._cli("/export terse"))
        if script:
            self._cli(script)

    # OSPF

    def _ospf_area_name(self, area: str) -> str:
        names = [
            row["name"]
            for row in self._rows("/routing/ospf/area/print terse")
            if row.get("area-id") == area
        ]
        if len(names) != 1:
            raise ValueError(f"No single OSPF area {area} on {self.node!r}")
        return names[0]

    def ospf_area_ranges(self, area: str) -> dict[str, bool]:
        name = self._ospf_area_name(area)
        return {
            row["prefix"]: row.get("advertise", "yes") == "no"
            for row in self._rows("/routing/ospf/area/range/print terse")
            if row.get("area") == name
        }

    def set_ospf_area_ranges(
        self, area: str, prefixes: list[str], suppress: bool
    ) -> None:
        name = self._ospf_area_name(area)
        lines = [
            f'/routing ospf area range add area="{name}" prefix={ipaddress.IPv4Network(p)} advertise=no'
            if suppress
            else f'/routing ospf area range remove [find where area="{name}" and prefix="{ipaddress.IPv4Network(p)}"]'
            for p in prefixes
        ]
        if lines:
            self._cli("; ".join(lines))

    def ospf_summary_lsa_ids(self, area: str) -> set[str]:
        name = self._ospf_area_name(area)
        return {
            row["id"]
            for row in self._rows("/routing/ospf/lsa/print terse")
            if row.get("area") in {name, area}
            and row.get("type") in {"summary-network", "inter-area-prefix"}
            and int(row.get("age", "3600")) < 3600
        }

    def ospf_neighbors(self) -> list[OspfNeighbor]:
        return [
            OspfNeighbor(
                row["router-id"],
                row["address"],
                re.split(r"[%@,]", row.get("interface") or row.get("iface") or "")[0],
                row.get("state", ""),
            )
            for row in self._rows("/routing/ospf/neighbor/print terse")
            if "router-id" in row and "address" in row
        ]

    def ospf_bindings(self) -> dict[str, tuple[str, ...]]:
        addresses = self._rows("/ip/address/print terse")
        bindings = {}
        for row in self._ospf_templates():
            ports = {name for name in row.get("interfaces", "").split(",") if name}
            networks = [
                ipaddress.ip_network(prefix, strict=False)
                for prefix in row.get("networks", "").split(",")
                if prefix
            ]
            for address in addresses:
                if any(
                    ipaddress.ip_interface(address["address"]).ip in network
                    for network in networks
                ):
                    ports.add(address["interface"])
            bindings[row["id"]] = tuple(sorted(ports))
        return bindings

    def remove_ospf_binding(self, binding: str) -> None:
        if not re.fullmatch(r"\*[0-9A-Fa-f]+", binding):
            raise ValueError("OSPF interface template has no stable ID")
        self._cli(f"/routing ospf interface-template remove {binding}")

    def ospf_router_id(self) -> str | None:
        rows = self._rows(
            "/routing/ospf/instance/print terse where version=2 and !disabled"
        )
        return rows[0].get("router-id") if len(rows) == 1 else None

    def ospf_router_lsa_ids(self) -> set[str]:
        return {
            row["originator"]
            for row in self._rows("/routing/ospf/lsa/print terse where type=router")
            if "originator" in row
        }

    def ospf_neighbor_ids(self) -> set[str]:
        return {
            row["router-id"]
            for row in self._rows("/routing/ospf/neighbor/print terse")
            if "router-id" in row
        }

    def set_ospf_router_id(self, router_id: str) -> None:
        from ipaddress import IPv4Address

        self._cli(
            f"/routing ospf instance set [find version=2] router-id={IPv4Address(router_id)}; "
            "/routing ospf instance disable [find version=2]; :delay 2s; "
            "/routing ospf instance enable [find version=2]"
        )

    def ospf_routes(self) -> set[str]:
        return {
            row["dst-address"]
            for row in self._rows(
                "/routing/route/print terse where ospf and active and afi=ip"
            )
            if "dst-address" in row
        }

    def connected_routes(self) -> set[str]:
        return {
            row["dst-address"]
            for row in self._rows(
                "/routing/route/print terse where connect and active and afi=ip"
            )
            if "dst-address" in row
        }

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

    # OSPF interface network type

    _OSPF_TYPE_TO_ROS = {
        "broadcast": "broadcast",
        "point-to-point": "ptp",
        "non-broadcast": "nbma",
    }
    _OSPF_TYPE_FROM_ROS = {
        "broadcast": "broadcast",
        "ptp": "point-to-point",
        "nbma": "non-broadcast",
    }

    def _ospf_templates(self) -> list[dict[str, str]]:
        # Terse output omits internal IDs. Prefix each row with its find ID.
        output = self._cli(
            ":foreach templateId in=[/routing ospf interface-template find where !disabled] do={"
            ':put (".id=" . $templateId); '
            "/routing ospf interface-template print terse from=$templateId}"
        )
        parts = re.split(r"(?m)^\.id=(\*[0-9A-Fa-f]+)\s*\r?$", output)
        rows = []
        for template_id, terse in zip(parts[1::2], parts[2::2], strict=True):
            for row in routeros_terse_rows(terse):
                rows.append({**row, "id": template_id})
        return rows

    def _ospf_template(self, interface: str) -> dict[str, str]:
        port = self._port(interface)
        addresses = [
            ipaddress.ip_interface(cidr).ip
            for cidr in self.interface_addresses(interface)
        ]
        for row in self._ospf_templates():
            if row.get("disabled") == "yes":
                continue
            names = row.get("interfaces", "").split(",")
            networks = row.get("networks", "").split(",")
            if port in names or any(
                address in ipaddress.ip_network(network, strict=False)
                for network in networks
                if network
                for address in addresses
            ):
                if not re.fullmatch(r"\*[0-9A-Fa-f]+", row.get("id", "")):
                    raise RuntimeError("OSPF interface template has no stable ID")
                return row
        raise RuntimeError(f"No OSPF interface template for {interface} on {self.node}")

    def ospf_network_type(self, interface: str) -> str:
        row = self._ospf_template(interface)
        raw = row.get("type") or "broadcast"
        return self._OSPF_TYPE_FROM_ROS.get(raw, raw)

    def ospf_interface_area_type(self, interface: str) -> tuple[str, str]:
        name = self._ospf_template(interface)["area"]
        rows = self._rows(f'/routing/ospf/area/print terse where name="{name}"')
        if len(rows) != 1 or "area-id" not in rows[0]:
            raise RuntimeError(f"No single OSPF area {name} on {self.node}")
        return rows[0]["area-id"], rows[0].get("type", "default").replace(
            "default", "normal"
        )

    def set_ospf_interface_area_type(self, interface: str, area_type: str) -> None:
        if area_type not in {"normal", "stub"}:
            raise ValueError(f"Unsupported OSPF area type: {area_type}")
        name = self._ospf_template(interface)["area"]
        areas = self._rows(f'/routing/ospf/area/print terse where name="{name}"')
        if len(areas) != 1 or "instance" not in areas[0]:
            raise RuntimeError(
                f"No single OSPF instance for area {name} on {self.node}"
            )
        instance = areas[0]["instance"]
        value = "default" if area_type == "normal" else "stub"
        # RouterOS retains Full neighbors after an area-type edit. Restart the
        # owning instance so its Hellos use the new ExternalRoutingCapability.
        self._cli(
            f'/routing ospf area set [find where name="{name}"] type={value}; '
            f'/routing ospf instance disable [find where name="{instance}"]; '
            f':delay 2s; /routing ospf instance enable [find where name="{instance}"]'
        )

    def _ospf_single_interface_template(self, interface: str) -> dict[str, str]:
        row = self._ospf_template(interface)
        template_id = row["id"]
        port = self._port(interface)
        ports = {name for name in row.get("interfaces", "").split(",") if name}
        networks = [
            ipaddress.ip_network(network, strict=False)
            for network in row.get("networks", "").split(",")
            if network
        ]
        if networks:
            for address in self._rows("/ip/address/print terse"):
                ip = ipaddress.ip_interface(address["address"]).ip
                if any(ip in network for network in networks):
                    ports.add(address["interface"])
        if ports != {port}:
            # A preceding copy does not rebind existing RouterOS OSPF interfaces.
            # Reject shared bindings before changing any other adjacency.
            raise RuntimeError(
                f"OSPF template {template_id} on {self.node} covers other interfaces: "
                f"{', '.join(sorted(ports - {port}))}"
            )
        return row

    def set_ospf_network_type(self, interface: str, network_type: str) -> None:
        template_id = self._ospf_single_interface_template(interface)["id"]
        ros_type = self._OSPF_TYPE_TO_ROS.get(network_type, network_type)
        self._cli(f"/routing ospf interface-template set {template_id} type={ros_type}")

    def ospf_interface_timers(self, interface: str) -> tuple[int, int]:
        row = self._ospf_template(interface)
        timers = []
        for key, default in (("hello-interval", "10s"), ("dead-interval", "40s")):
            raw = row.get(key, default)
            parts = list(_DURATION.finditer(raw))
            if not parts or "".join(part[0] for part in parts) != raw:
                raise RuntimeError(f"Unrecognized OSPF timer {raw!r} on {self.node}")
            seconds = sum(int(part[1]) * _UNITS[part[2]] for part in parts)
            if seconds < 1 or int(seconds) != seconds:
                raise RuntimeError(f"OSPF timer is not whole seconds: {raw!r}")
            timers.append(int(seconds))
        return tuple(timers)

    def set_ospf_interface_timers(self, interface: str, hello: int, dead: int) -> None:
        template_id = self._ospf_single_interface_template(interface)["id"]
        self._cli(
            f"/routing ospf interface-template set {template_id} "
            f"hello-interval={int(hello)}s dead-interval={int(dead)}s"
        )

    def ospf_interface_neighbor_full(self, interface: str) -> bool:
        port = self._port(interface)

        def row_port(row: dict[str, str]) -> str:
            # RouterOS may append a VRF or address suffix, as in ``ether2%main``.
            return re.split(r"[%@,]", row.get("interface") or row.get("iface") or "")[0]

        return any(
            row.get("state", "").lower().startswith("full") and row_port(row) == port
            for row in self._rows("/routing/ospf/neighbor/print terse")
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

    def block_ospf_transport(self) -> str:
        position = " place-before=0" if self._filter_rules() else ""
        rule_id = self._cli(
            ":put [/ip firewall filter add chain=input protocol=ospf action=drop"
            + position
            + "]"
        ).strip()
        if not re.fullmatch(r"\*[0-9A-Fa-f]+", rule_id):
            raise ValueError(f"RouterOS omitted the firewall rule ID: {rule_id!r}")
        return rule_id

    def ospf_transport_blocked(self) -> bool:
        return any(
            row.get("chain") == "input"
            and row.get("protocol") in {"ospf", "89"}
            and row.get("action") == "drop"
            and row.get("disabled", "no") == "no"
            for row in self._rows("/ip/firewall/filter/print terse where !disabled")
        )

    def unblock_ospf_transport(self, filter_id: str) -> None:
        if not re.fullmatch(r"\*[0-9A-Fa-f]+", filter_id):
            raise ValueError(f"Invalid RouterOS firewall rule ID: {filter_id!r}")
        self._cli(f"/ip firewall filter remove {filter_id}")

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

    def ipv4_addresses(self) -> set[str]:
        return {
            row["address"].split("/")[0]
            for row in self._rows("/ip/address/print terse")
            if "address" in row
        }

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

    def set_queue_bandwidth(self, name: str, rate_kbps: int, ceil_kbps: int) -> None:
        self._cli(
            f'/queue tree set [find name="{name}"] '
            f"limit-at={rate_kbps}k max-limit={ceil_kbps}k"
        )

    def queue_state(self, name: str) -> dict[str, int]:
        # get returns numeric rates; print abbreviates rates and wraps stats.
        fields = ("limit-at", "max-limit", "bytes", "packets", "dropped")
        output = self._cli(
            "; ".join(
                f':put [/queue tree get [find name="{name}"] {field}]'
                for field in fields
            )
        )
        rate, ceil, sent, packets, drops = (int(value) for value in output.split())
        return {
            "rate_kbps": rate // 1000,
            "ceil_kbps": ceil // 1000,
            "bytes": sent,
            "packets": packets,
            "drops": drops,
        }

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

    def vpn_import_targets(self, vrf: str) -> set[str]:
        rows = self._rows(f'/routing/bgp/vpn/print terse where vrf="{vrf}"')
        if len(rows) != 1:
            raise ValueError(f"Expected one BGP VPN policy for {self.node}:{vrf}")
        return set(filter(None, rows[0].get("import.route-targets", "").split(",")))

    def set_vpn_import_targets(self, vrf: str, targets: set[str]) -> None:
        self._cli(
            f'/routing bgp vpn set [find vrf="{vrf}"] '
            f'import.route-targets="{",".join(sorted(targets))}"'
        )

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
            f"({_address_match('reply-dst-address', address)})]; "
            "/ip firewall connection remove [find protocol=tcp and "
            f"({_address_match('dst-address', address)})]"
        )

    def nat_mapping_count(self, address: str) -> int:
        return routeros_count(
            self._cli(
                "/ip/firewall/connection/print count-only where protocol=tcp and "
                f"({_address_match('reply-dst-address', address)})"
            )
        )

    # ------------------------------------------------------------------
    # IS-IS metric style
    # ------------------------------------------------------------------

    def isis_metric_style(self) -> str:
        values = {
            row.get("metric-type")
            for row in self._rows("/routing/isis/instance/print terse")
            if row.get("metric-type")
        }
        if len(values) != 1:
            raise ValueError(
                f"No single IS-IS metric-type on RouterOS router {self.node!r}"
            )
        value = str(next(iter(values)))
        # RouterOS names old-style (narrow) metrics ``old``.
        return "narrow" if value == "old" else value

    def set_isis_metric_style(self, style: str) -> None:
        ros_style = "old" if style == "narrow" else style
        self._cli(f"/routing isis instance set [find] metric-type={ros_style}")

    # ------------------------------------------------------------------
    # IS-IS level (instance is-type / circuit levels)
    # ------------------------------------------------------------------

    _ISIS_LEVEL_TO_ROS = {
        "level-1": "l1",
        "level-1-2": "l1-2",
        "level-2-only": "l2",
    }
    _ISIS_LEVEL_FROM_ROS = {
        "l1": "level-1",
        "l1-2": "level-1-2",
        "l2": "level-2-only",
    }

    def isis_level(self) -> str:
        values = {
            row.get("levels")
            for row in self._rows("/routing/isis/interface-template/print terse")
            if row.get("levels")
        }
        if len(values) != 1:
            raise ValueError(
                f"No single IS-IS interface level on RouterOS router {self.node!r}"
            )
        value = str(next(iter(values)))
        return self._ISIS_LEVEL_FROM_ROS.get(value, value)

    def set_isis_level(self, level: str) -> None:
        ros_level = self._ISIS_LEVEL_TO_ROS.get(level)
        if ros_level is None:
            raise ValueError(f"Unsupported IS-IS level {level!r}")
        new_metric = {"l1": "ptp.l1.metric", "l2": "ptp.l2.metric"}.get(ros_level)
        old_metrics = [
            key for key in ("ptp.l1.metric", "ptp.l2.metric") if key != new_metric
        ]
        for row in self._rows("/routing/isis/interface-template/print terse"):
            iface = row.get("interfaces")
            if not iface:
                continue
            selector = f'[find interfaces="{iface}"]'
            metric = row.get("ptp.l1.metric") or row.get("ptp.l2.metric")
            for key in old_metrics:
                if row.get(key):
                    self._cli(f"/routing isis interface-template set {selector} !{key}")
            parts = [f"levels={ros_level}"]
            if metric and new_metric:
                parts.append(f"{new_metric}={metric}")
            self._cli(
                f"/routing isis interface-template set {selector} {' '.join(parts)}"
            )
