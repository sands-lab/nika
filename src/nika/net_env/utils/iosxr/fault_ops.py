"""IOS XR (XRd) provider for failure operations on native routers."""

from __future__ import annotations

import ipaddress
import re

from nika.net_env.utils.iosxr.common import (
    build_xr_config_command,
    build_xr_file_command,
    iosxr_interface,
)
from nika.problems.support.router_ops import OspfNeighbor, RouterFaultOps
from nika.service.lab.iosxr_api import (
    CLI_COMMAND,
    _NETWORK_LINE,
    _rewrite_router_bgp_asn,
)

# Interface ACL a misconfigured transit filter would carry; the name says
# nothing about what it drops.
_FILTER_ACL = "TRANSIT-FILTER"
_OSPF_FILTER_ACL = "CONTROL-FILTER"
# Loopback holding an originated prefix; Loopback0 is the router ID.
_ORIGIN_LOOPBACK = "Loopback1"
_ADDRESSED_INTERFACE = re.compile(
    r"^(GigabitEthernet\S+)\s+(\d+\.\d+\.\d+\.\d+)\s", re.MULTILINE
)
_ROUTE_PREFIX = re.compile(
    r"^(\S+(?: [A-Z][A-Z0-9]?)?)\s+(\d+\.\d+\.\d+\.\d+/\d+)\s", re.MULTILINE
)
_ROUTER_BGP = re.compile(r"^router bgp (\d+)", re.MULTILINE)
_NEIGHBOR_BLOCK = re.compile(r"^ neighbor (\S+)\n(.*?)^ !$", re.MULTILINE | re.DOTALL)
_STATIC_NULL0 = re.compile(r"^\s+(\d+\.\d+\.\d+\.\d+/\d+)\s+Null0\s*$", re.MULTILINE)


def _advertises_local(output: str, prefix: str) -> bool:
    """Whether ``advertised neighbor <peer> summary`` lists ``prefix`` from Local."""
    return any(
        line.split()[:1] == [prefix] and line.split()[2:3] == ["Local"]
        for line in output.splitlines()
    )


# XR commits and configuration replacement can take minutes on a loaded host.
_CONFIG_TIMEOUT_SEC = 300


def _replace_command(running: str) -> str:
    """Replace the running configuration with ``show running-config`` output."""
    lines = running.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("!! IOS XR")), None
    )
    if start is None or not any(line == "end" for line in lines[start:]):
        raise ValueError("Unrecognized IOS XR running configuration")
    config = "\n".join(lines[start:]) + "\n"
    return build_xr_file_command(config, 'xrreplace "$xr_config_file"')


class IosxrFaultOps(RouterFaultOps):
    profile = "iosxr"

    def _show(self, command: str) -> str:
        output = self.runtime.exec(
            self.node, CLI_COMMAND.format(command=command), timeout=90
        )
        if (
            output.startswith("[TIMEOUT]")
            or re.search(r"invalid input|syntax error|unknown command", output, re.I)
            or (command.startswith("show ") and not output.strip())
        ):
            raise RuntimeError(f"IOS XR command failed on {self.node}: {command}")
        return output

    def _apply(self, *lines: str) -> None:
        self._exec(build_xr_config_command(lines))

    def bgp_asn(self) -> int:
        from nika.net_env.vendor_validation import _parse_iosxr_bgp_summary

        asn, _ = _parse_iosxr_bgp_summary(self._show("show bgp summary"))
        if asn is None:
            raise ValueError("IOS XR BGP summary omitted the local AS number")
        return asn

    def bgp_prefix_paths(self, prefix: str) -> list[dict]:
        from nika.net_env.vendor_validation import iosxr_bgp_prefix_paths

        prefix = str(ipaddress.IPv4Network(prefix))
        return iosxr_bgp_prefix_paths(
            self._show(f"show bgp ipv4 unicast {prefix}"), prefix
        )

    def bgp_table(self) -> dict[str, list[dict]]:
        from nika.net_env.vendor_validation import iosxr_bgp_table_prefixes

        prefixes = iosxr_bgp_table_prefixes(self._show("show bgp ipv4 unicast"))
        return {prefix: self.bgp_prefix_paths(prefix) for prefix in prefixes}

    def set_bgp_asn(self, asn: int) -> None:
        current = self.bgp_asn()
        if current == asn:
            return
        config = self._show("show running-config router bgp")
        rebuilt = _rewrite_router_bgp_asn(config, current, asn)
        self._apply(f"no router bgp {current}")
        self._apply(*rebuilt)

    def bgp_networks(self) -> list[str]:
        return _NETWORK_LINE.findall(self._show("show running-config router bgp"))

    def withdraw_bgp_network(self, prefix: str) -> None:
        self._apply(
            f"router bgp {self.bgp_asn()}",
            " address-family ipv4 unicast",
            f"  no network {ipaddress.ip_network(prefix)}",
            " !",
            "!",
        )

    def _exec(self, command: str) -> str:
        result = self.runtime.exec(self.node, command, timeout=_CONFIG_TIMEOUT_SEC)
        if re.search(
            r"^\[TIMEOUT\]|invalid input|syntax error|failed to commit|no such file or directory",
            result,
            re.I | re.M,
        ):
            raise RuntimeError(
                f"IOS XR configuration operation failed on {self.node}: {result.strip()}"
            )
        return result

    def save_config(self) -> str:
        saved = self._exec("/pkg/bin/xr_cli 'show running-config'")
        if not saved.strip():
            raise ValueError(f"Unreadable IOS XR configuration on {self.node!r}")
        return saved

    def restore_config(self, saved: str) -> None:
        # XRd cannot remove one BGP instance and create another in one commit;
        # the replace then leaves no BGP at all. Remove a renumbered one first.
        running = _ROUTER_BGP.search(self.save_config())
        wanted = _ROUTER_BGP.search(saved)
        if running and (wanted is None or running[1] != wanted[1]):
            self._exec(
                build_xr_file_command(
                    f"no router bgp {running[1]}\nend\n",
                    'xrapply "$xr_config_file"',
                )
            )
        self._exec(_replace_command(saved))

    # Routing sessions

    def bgp_established_peers(self) -> set[str]:
        from nika.net_env.vendor_validation import _parse_iosxr_bgp_summary

        _, peers = _parse_iosxr_bgp_summary(self._show("show bgp summary"))
        return {peer for peer, state in peers.items() if state.established}

    def reset_bgp_session(self, peer: str) -> None:
        self._show(f"clear bgp {ipaddress.ip_address(peer)}")

    def bgp_received_prefix_count(self, peer: str) -> int:
        output = self._show("show bgp ipv4 unicast summary")
        for line in output.splitlines():
            fields = line.split()
            if fields[:1] == [str(ipaddress.IPv4Address(peer))]:
                return (
                    int(fields[-1].rstrip("!"))
                    if fields[-1].rstrip("!").isdigit()
                    else 0
                )
        raise ValueError(f"IOS XR omitted BGP peer {peer}")

    def bgp_prefix_limit(self, peer: str) -> int | None:
        peers = dict(
            _NEIGHBOR_BLOCK.findall(self._show("show running-config router bgp"))
        )
        match = re.search(r"^   maximum-prefix (\d+)\b", peers.get(peer, ""), re.M)
        return int(match[1]) if match else None

    def set_bgp_prefix_limit(self, peer: str, limit: int) -> None:
        self._apply(
            f"router bgp {self.bgp_asn()}",
            f" neighbor {ipaddress.IPv4Address(peer)}",
            "  address-family ipv4 unicast",
            f"   maximum-prefix {int(limit)}",
            "  !",
            " !",
            "!",
        )

    def bgp_prefix_limit_exceeded(self, peer: str) -> bool:
        output = self._show(f"show bgp neighbor {ipaddress.IPv4Address(peer)}")
        return "peer had exceeded the max. no. of prefixes configured" in output.lower()

    def bgp_last_notification(self, peer: str) -> str | None:
        output = self._show(f"show bgp neighbor {ipaddress.IPv4Address(peer)}")
        match = re.search(
            r"Time since last notification received from neighbor:[^\n]*\n"
            r"(.*?)(?=\n\s*(?:Time since|Notification data|Last reset)|\Z)",
            output,
            re.I | re.S,
        )
        return match[1].strip() if match else None

    def permit_bgp_prefixes(self, prefix: str, length: int) -> None:
        config = self.save_config()
        policies = {
            policy.split()[1]
            for lines in self._ebgp_policies().values()
            for policy in lines
        }
        lines = []
        for name, body in re.findall(
            r"^route-policy (\S+)\n(.*?)^end-policy", config, re.M | re.S
        ):
            if name in policies:
                lines += [
                    f"route-policy {name}",
                    f" if destination in ({ipaddress.ip_network(prefix)} eq {int(length)}) then",
                    "  pass",
                    " else",
                    *body.splitlines(),
                    " endif",
                    "end-policy",
                    "!",
                ]
        # A router with no bound eBGP policy filters nothing to open.
        if lines:
            self._apply(*lines)

    def advertise_bgp_prefixes(self, prefixes: list[str]) -> None:
        self._apply(
            "router static",
            " address-family ipv4 unicast",
            *(f"  {ipaddress.ip_network(prefix)} Null0" for prefix in prefixes),
            " !",
            "!",
            f"router bgp {self.bgp_asn()}",
            " address-family ipv4 unicast",
            *(f"  network {ipaddress.ip_network(prefix)}" for prefix in prefixes),
            " !",
            "!",
        )

    def has_bgp_route(self, prefix: str) -> bool:
        return bool(self.bgp_prefix_paths(prefix))

    def set_bgp_password(self, peer: str, password: str | None) -> None:
        peer = str(ipaddress.ip_address(peer))
        self._apply(
            f"router bgp {self.bgp_asn()}",
            f" neighbor {peer}",
            f"  password clear {password}" if password is not None else "  no password",
            " !",
            "!",
        )

    def bgp_password_configured(self, peer: str) -> bool:
        peers = dict(
            _NEIGHBOR_BLOCK.findall(self._show("show running-config router bgp"))
        )
        return bool(re.search(r"^  password .+$", peers.get(peer, ""), re.M))

    def bgp_received_prefixes(self, peer: str) -> set[str]:
        output = self._show(
            f"show bgp ipv4 unicast neighbors {ipaddress.ip_address(peer)} routes"
        )
        if not output.strip():
            raise ValueError(f"Unreadable BGP routes on {self.node!r}")
        return set(re.findall(r"\d+\.\d+\.\d+\.\d+/\d+", output))

    def remote_bgp_prefixes(self) -> list[str]:
        return sorted(
            match[2]
            for match in _ROUTE_PREFIX.finditer(self._show("show route ipv4 bgp"))
            if match[1] == "B"
        )

    def originate_bgp_prefix(self, prefix: str) -> None:
        net = ipaddress.ip_network(prefix, strict=False)
        address = net.network_address if net.prefixlen >= 31 else next(net.hosts())
        # The router answers for the prefix, as FRR's local route does, and a
        # BGP network statement sources it from that connected route.
        self._apply(
            f"interface {_ORIGIN_LOOPBACK}",
            f" ipv4 address {address} {net.netmask}",
            "!",
            f"router bgp {self.bgp_asn()}",
            " address-family ipv4 unicast",
            f"  network {net}",
            " !",
            "!",
        )

    def bgp_advertised_to(self, prefix: str) -> list[str]:
        # Only the router's own origination counts, not a path it relays.
        return sorted(
            peer
            for peer in self.bgp_established_peers()
            if _advertises_local(
                self._show(f"show bgp ipv4 unicast advertised neighbor {peer} summary"),
                prefix,
            )
        )

    def _ebgp_neighbors(self) -> list[tuple[str, str]]:
        """Neighbor blocks whose remote AS differs from the local AS."""
        config = self._show("show running-config router bgp")
        local = _ROUTER_BGP.search(config)
        return [
            (peer, body)
            for peer, body in _NEIGHBOR_BLOCK.findall(config)
            if local
            and (remote := re.search(r"^  remote-as (\d+)$", body, re.M))
            and remote[1] != local[1]
        ]

    def _ebgp_policies(self) -> dict[str, list[str]]:
        """eBGP neighbor -> its IPv4 unicast ``route-policy`` lines."""
        policies = {}
        for peer, body in self._ebgp_neighbors():
            unicast = re.search(
                r"^  address-family ipv4 unicast\n(.*?)^  !$", body, re.M | re.S
            )
            policies[peer] = re.findall(
                r"^   (route-policy \S+ (?:in|out))$",
                unicast[1] if unicast else "",
                re.MULTILINE,
            )
        return policies

    def drop_bgp_session_policy(self) -> None:
        # XR follows RFC 8212: an eBGP session without a policy exchanges no
        # routes, and the session itself stays up.
        lines = [f"router bgp {self.bgp_asn()}"]
        for peer, policies in self._ebgp_policies().items():
            if policies:
                lines += [f" neighbor {peer}", "  address-family ipv4 unicast"]
                lines += [f"   no {policy}" for policy in policies]
                lines += ["  !", " !"]
        self._apply(*lines, "!")

    def bgp_session_policy_dropped(self) -> bool:
        policies = self._ebgp_policies()
        return bool(policies) and not any(policies.values())

    def enable_bgp_ttl_security(self) -> None:
        # IOS XR accepts ttl-security only on external neighbors.
        peers = self._ebgp_neighbors()
        if not peers:
            raise ValueError(f"No eBGP neighbors on {self.node!r}")
        lines = [f"router bgp {self.bgp_asn()}"]
        for peer, _ in peers:
            lines += [f" neighbor {peer}", "  ttl-security", " !"]
        self._apply(*lines, "!")
        self._show("clear bgp *")

    def bgp_ttl_security_enabled(self) -> bool:
        peers = self._ebgp_neighbors()
        return bool(peers) and all(
            re.search(r"^  ttl-security$", body, re.M) for _, body in peers
        )

    # OSPF

    def ospf_area_ranges(self, area: str) -> dict[str, bool]:
        current = None
        ranges = {}
        for line in self._show("show running-config router ospf").splitlines():
            if match := re.match(r"^ area (\S+)", line):
                token = match[1]
                current = str(
                    ipaddress.IPv4Address(int(token) if token.isdigit() else token)
                )
            elif current == area and (match := re.match(r"^  range (\S+)(.*)", line)):
                options = match[2].split()
                prefix = match[1] if "/" in match[1] else f"{match[1]}/{options[0]}"
                ranges[str(ipaddress.IPv4Network(prefix))] = "not-advertise" in options
        return ranges

    def set_ospf_area_ranges(
        self, area: str, prefixes: list[str], suppress: bool
    ) -> None:
        config = self._show("show running-config router ospf")
        processes = re.findall(r"^router ospf (\S+)$", config, re.M)
        if len(processes) != 1:
            raise ValueError(f"No single OSPF process on {self.node!r}")
        # XR requires edits to retain the configured decimal or dotted area spelling.
        areas = {
            str(ipaddress.IPv4Address(int(token) if token.isdigit() else token)): token
            for token in re.findall(r"^ area (\S+)$", config, re.M)
        }
        if area not in areas:
            raise ValueError(f"No OSPF area {area} on {self.node!r}")
        lines = []
        for prefix in prefixes:
            network = ipaddress.IPv4Network(prefix)
            value = str(network)
            lines.append(
                f"  range {value} not-advertise" if suppress else f"  no range {value}"
            )
        if lines:
            self._apply(
                f"router ospf {processes[0]}",
                f" area {areas[area]}",
                *lines,
                " !",
                "!",
            )

    def ospf_summary_lsa_ids(self, area: str) -> set[str]:
        output = self._show("show ospf database summary")
        current = None
        age = None
        result = set()
        for line in output.splitlines():
            if match := re.search(r"Summary Net Link States \(Area ([^)]+)\)", line):
                token = match[1]
                current = str(
                    ipaddress.IPv4Address(int(token) if token.isdigit() else token)
                )
            elif match := re.search(r"LS age:\s*(\d+)", line):
                age = int(match[1])
            elif current == area and age is not None and age < 3600:
                if match := re.search(r"Link State ID:\s*(\d+\.\d+\.\d+\.\d+)", line):
                    result.add(match[1])
        return result

    def ospf_neighbors(self) -> list[OspfNeighbor]:
        output = self._show("show ospf neighbor")
        return [
            OspfNeighbor(
                router_id,
                address,
                re.sub(r"^Gi(?=\d)", "GigabitEthernet", interface),
                state,
            )
            for router_id, state, address, interface in re.findall(
                r"^\s*(\d+\.\d+\.\d+\.\d+)\s+\d+\s+(\S+)\s+"
                r"(?:-\s+)?\S+\s+(\d+\.\d+\.\d+\.\d+)\s+(\S+)",
                output,
                re.M,
            )
        ]

    def ospf_bindings(self) -> dict[str, tuple[str, ...]]:
        bindings = {}
        process = area = None
        for line in self._show("show running-config router ospf").splitlines():
            if match := re.fullmatch(r"router ospf (\S+)", line):
                process, area = match[1], None
            elif match := re.fullmatch(r" area (\S+)", line):
                area = match[1]
            elif match := re.fullmatch(r"  interface (\S+)", line):
                if process and area:
                    interface = match[1]
                    bindings[f"{process}|{area}|{interface}"] = (interface,)
        return bindings

    def remove_ospf_binding(self, binding: str) -> None:
        process, area, interface = binding.split("|")
        self._apply(
            f"router ospf {process}",
            f" area {area}",
            f"  no interface {interface}",
            " !",
            "!",
        )

    def ospf_router_id(self) -> str | None:
        match = re.search(
            r'Routing Process "ospf [^"]+" with ID (\S+)', self._show("show ospf")
        )
        return match[1] if match else None

    def ospf_router_lsa_ids(self) -> set[str]:
        # Router-LSA rows begin with Link ID and Advertising Router.
        return set(
            re.findall(
                r"^\s*\d+\.\d+\.\d+\.\d+\s+(\d+\.\d+\.\d+\.\d+)\s+",
                self._show("show ospf database router"),
                re.M,
            )
        )

    def ospf_neighbor_ids(self) -> set[str]:
        return set(
            re.findall(
                r"^\s*(\d+\.\d+\.\d+\.\d+)\s+\d+\s+",
                self._show("show ospf neighbor"),
                re.M,
            )
        )

    def set_ospf_router_id(self, router_id: str) -> None:
        config = self._show("show running-config router ospf")
        processes = re.findall(r"^router ospf (\S+)$", config, re.M)
        if len(processes) != 1:
            raise ValueError(f"No single OSPF process on {self.node!r}")
        self._apply(
            f"router ospf {processes[0]}",
            f" router-id {ipaddress.IPv4Address(router_id)}",
            "!",
        )
        self._show(f"clear ospf {processes[0]} process")

    def ospf_routes(self) -> set[str]:
        return {
            m[2]
            for m in _ROUTE_PREFIX.finditer(self._show("show route ipv4 ospf"))
            if m[1] in {"O", "O IA", "O E1", "O E2"}
        }

    def connected_routes(self) -> set[str]:
        return {
            m[2]
            for m in _ROUTE_PREFIX.finditer(self._show("show route ipv4 connected"))
            if m[1] == "C"
        }

    # OSPF interface network type

    def _xr_interface(self, interface: str) -> str:
        return (
            iosxr_interface(interface)
            if re.fullmatch(r"eth\d+|lo", interface)
            else interface
        )

    def _ospf_area_of(self, xr_interface: str) -> tuple[str, str]:
        config = self._show("show running-config router ospf")
        process = "CORE"
        area = None
        for line in config.splitlines():
            if match := re.match(r"^router ospf (\S+)", line):
                process = match[1]
            elif match := re.match(r"^ area (\S+)\s*$", line):
                area = match[1]
            elif match := re.match(r"^  interface (\S+)\s*$", line):
                if match[1] == xr_interface and area is not None:
                    return process, area
        raise RuntimeError(f"No OSPF area binding for {xr_interface} on {self.node}")

    def ospf_network_type(self, interface: str) -> str:
        xr_if = self._xr_interface(interface)
        output = self._show(f"show ospf interface {xr_if}")
        match = re.search(r"Network Type (\S+)", output)
        if not match:
            raise RuntimeError(f"No OSPF network type for {xr_if} on {self.node}")
        token = match[1].strip(",").lower().replace("_", "-")
        if token in {"point-to-point", "pointtopoint"}:
            return "point-to-point"
        if token in {"non-broadcast", "nonbroadcast"}:
            return "non-broadcast"
        return "broadcast" if token == "broadcast" else token

    def ospf_interface_area_type(self, interface: str) -> tuple[str, str]:
        process, area = self._ospf_area_of(self._xr_interface(interface))
        config = self._show(f"show running-config router ospf {process} area {area}")
        match = re.search(r"^  (stub|nssa)\b", config, re.M)
        dotted = str(ipaddress.IPv4Address(int(area) if area.isdigit() else area))
        return dotted, match[1] if match else "normal"

    def set_ospf_interface_area_type(self, interface: str, area_type: str) -> None:
        if area_type not in {"normal", "stub"}:
            raise ValueError(f"Unsupported OSPF area type: {area_type}")
        process, area = self._ospf_area_of(self._xr_interface(interface))
        self._apply(
            f"router ospf {process}",
            f" area {area}",
            "  stub" if area_type == "stub" else "  no stub",
            " !",
            "!",
        )

    def ospf_interface_timers(self, interface: str) -> tuple[int, int]:
        xr_if = self._xr_interface(interface)
        output = self._show(f"show ospf interface {xr_if}")
        match = re.search(
            r"Timer intervals configured, Hello (\d+), Dead (\d+)", output
        )
        if not match:
            raise RuntimeError(f"No OSPF timers for {xr_if} on {self.node}")
        return int(match[1]), int(match[2])

    def set_ospf_interface_timers(self, interface: str, hello: int, dead: int) -> None:
        xr_if = self._xr_interface(interface)
        process, area = self._ospf_area_of(xr_if)
        self._apply(
            f"router ospf {process}",
            f" area {area}",
            f"  interface {xr_if}",
            f"   hello-interval {int(hello)}",
            f"   dead-interval {int(dead)}",
            "  !",
            " !",
            "!",
        )

    def ospf_interface_neighbor_full(self, interface: str) -> bool:
        xr_if = self._xr_interface(interface)
        detail = self._show(f"show ospf interface {xr_if}")
        match = re.search(r"Adjacent neighbor count is (\d+)", detail, re.I)
        if match:
            return int(match[1]) > 0
        return any(
            neighbor.interface == xr_if and neighbor.state.lower().startswith("full")
            for neighbor in self.ospf_neighbors()
        )

    # Packet filters

    def _addressed_interfaces(self) -> list[str]:
        return _ADDRESSED_INTERFACE.findall(self._show("show ipv4 interface brief"))

    def block_bgp_transport(self) -> None:
        lines = [
            f"ipv4 access-list {_FILTER_ACL}",
            " 10 deny tcp any any eq bgp",
            " 20 deny tcp any eq bgp any",
            " 30 permit ipv4 any any",
            "!",
        ]
        for interface, _address in self._addressed_interfaces():
            lines += [
                f"interface {interface}",
                f" ipv4 access-group {_FILTER_ACL} ingress",
                f" ipv4 access-group {_FILTER_ACL} egress",
                "!",
            ]
        self._apply(*lines)
        # Drop the sessions now instead of after the hold timer, as FRR's
        # ``clear ip bgp *`` does; the filter keeps them down.
        self._show("clear bgp *")

    def bgp_transport_blocked(self) -> bool:
        acl = self._show(f"show running-config ipv4 access-list {_FILTER_ACL}")
        denies = all(
            re.search(rf"\bdeny tcp any {ports}", acl)
            for ports in (r"any eq (bgp|179)\b", r"eq (bgp|179) any\b")
        )
        applied = self._show("show running-config interface")
        return denies and all(
            f"ipv4 access-group {_FILTER_ACL} {direction}" in applied
            for direction in ("ingress", "egress")
        )

    def _ospf_filter_interfaces(self) -> list[str]:
        addressed = {interface for interface, _ in self._addressed_interfaces()}
        return sorted(
            {
                interface
                for ports in self.ospf_bindings().values()
                for interface in ports
            }
            & addressed
        )

    def block_ospf_transport(self) -> str:
        interfaces = self._ospf_filter_interfaces()
        if not interfaces:
            raise ValueError(f"No OSPF interfaces on {self.node!r}")
        config = self._show("show running-config")
        if f"ipv4 access-list {_OSPF_FILTER_ACL}" in config:
            raise ValueError(f"ACL {_OSPF_FILTER_ACL} already exists on {self.node!r}")
        for interface, body in re.findall(
            r"^interface (\S+)\n(.*?)(?=^!|\Z)", config, re.M | re.S
        ):
            if interface in interfaces and re.search(
                r"^ ipv4 access-group \S+ ingress", body, re.M
            ):
                raise ValueError(f"{self.node}:{interface} already has an ingress ACL")
        lines = [
            f"ipv4 access-list {_OSPF_FILTER_ACL}",
            " 10 deny ospf any any",
            " 20 permit ipv4 any any",
            "!",
        ]
        for interface in interfaces:
            lines += [
                f"interface {interface}",
                f" ipv4 access-group {_OSPF_FILTER_ACL} ingress",
                "!",
            ]
        self._apply(*lines)
        return _OSPF_FILTER_ACL

    def ospf_transport_blocked(self) -> bool:
        acl = self._show(f"show running-config ipv4 access-list {_OSPF_FILTER_ACL}")
        if not re.search(r"^ 10 deny (?:ospf|89) any any\s*$", acl, re.M):
            return False
        config = self._show("show running-config interface")
        applied = {
            interface
            for interface, body in re.findall(
                r"^interface (\S+)\n(.*?)(?=^!|\Z)", config, re.M | re.S
            )
            if f" ipv4 access-group {_OSPF_FILTER_ACL} ingress" in body
        }
        interfaces = set(self._ospf_filter_interfaces())
        return bool(interfaces) and interfaces <= applied

    def unblock_ospf_transport(self, filter_id: str) -> None:
        if filter_id != _OSPF_FILTER_ACL:
            raise ValueError(f"Unknown IOS XR OSPF filter: {filter_id!r}")
        lines = []
        for interface in self._ospf_filter_interfaces():
            lines += [
                f"interface {interface}",
                f" no ipv4 access-group {_OSPF_FILTER_ACL} ingress",
                "!",
            ]
        self._apply(*lines)
        self._apply(f"no ipv4 access-list {_OSPF_FILTER_ACL}")

    # Routes

    def ipv4_addresses(self) -> set[str]:
        return set(
            re.findall(
                r"^\S+\s+(\d+\.\d+\.\d+\.\d+)\s",
                self._show("show ipv4 interface brief"),
                re.M,
            )
        )

    def add_blackhole_route(self, prefix: str) -> None:
        self._apply(
            "router static",
            " address-family ipv4 unicast",
            f"  {ipaddress.ip_network(prefix, strict=False)} Null0",
            " !",
            "!",
        )

    def blackhole_routes(self) -> list[str]:
        return sorted(
            set(_STATIC_NULL0.findall(self._show("show running-config router static")))
        )

    # IS-IS metric style

    def _isis_config(self) -> tuple[str, str]:
        config = self._show("show running-config router isis")
        processes = re.findall(r"^router isis (\S+)$", config, re.M)
        if len(processes) != 1:
            raise ValueError(f"No single IS-IS process on {self.node!r}")
        return processes[0], config

    def isis_metric_style(self) -> str:
        _, cfg = self._isis_config()
        match = re.search(r"^\s*metric-style (\S+)", cfg, re.MULTILINE)
        return match[1] if match else "wide"

    def set_isis_metric_style(self, style: str) -> None:
        process, _ = self._isis_config()
        self._apply(
            f"router isis {process}",
            " address-family ipv4 unicast",
            f"  metric-style {style}",
            " !",
            "!",
        )

    # IS-IS level (instance is-type / circuit levels)

    def isis_level(self) -> str:
        _, cfg = self._isis_config()
        match = re.search(r"^\s*is-type (\S+)", cfg, re.MULTILINE)
        return match[1] if match else "level-1-2"

    def set_isis_level(self, level: str) -> None:
        process, cfg = self._isis_config()
        lines = [f"router isis {process}", f" is-type {level}"]
        current = None
        for line in cfg.splitlines():
            if match := re.match(r"^ interface (\S+)$", line):
                current = match[1]
            elif current and re.match(r"^  circuit-type ", line):
                lines.extend(
                    [
                        f" interface {current}",
                        f"  circuit-type {level}",
                        " !",
                    ]
                )
                current = None
        lines.append("!")
        self._apply(*lines)

    def isis_hello_authentication(self) -> dict[str, str]:
        _, config = self._isis_config()
        circuits = {}
        for match in re.finditer(
            r"^ interface (\S+)\n(.*?)(?=^ !|^!|\Z)", config, re.M | re.S
        ):
            interface, block = match.groups()
            if re.search(r"^  passive\s*$", block, re.M):
                continue
            auth = re.search(r"^  hello-password (\S+)", block, re.M)
            mode = auth[1] if auth else "none"
            circuits[interface] = "md5" if mode == "hmac-md5" else mode
        return circuits

    def set_isis_hello_password(self, password: str | None) -> None:
        process, _ = self._isis_config()
        lines = [f"router isis {process}"]
        for interface in self.isis_hello_authentication():
            lines.extend(
                [
                    f" interface {interface}",
                    f"  hello-password hmac-md5 clear {password}"
                    if password
                    else "  no hello-password",
                    " !",
                ]
            )
        lines.append("!")
        self._apply(*lines)

    # ------------------------------------------------------------------
    # BGP session reset (GTSM recover)
    # ------------------------------------------------------------------

    def reset_bgp(self) -> None:
        self._show("clear bgp *")
