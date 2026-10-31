"""IOS XR (XRd) provider for failure operations on native routers."""

from __future__ import annotations

import ipaddress
import re

from nika.net_env.utils.iosxr.common import build_xr_file_command
from nika.problems.support.router_ops import RouterFaultOps
from nika.service.lab.iosxr_api import CLI_COMMAND

# Interface ACL a misconfigured transit filter would carry; the name says
# nothing about what it drops.
_FILTER_ACL = "TRANSIT-FILTER"
# Loopback holding an originated prefix; Loopback0 is the router ID.
_ORIGIN_LOOPBACK = "Loopback1"
_ADDRESSED_INTERFACE = re.compile(
    r"^(GigabitEthernet\S+)\s+(\d+\.\d+\.\d+\.\d+)\s", re.MULTILINE
)
_ROUTE_PREFIX = re.compile(r"^(\S+)\s+(\d+\.\d+\.\d+\.\d+/\d+)\s", re.MULTILINE)
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
        return self.runtime.exec(
            self.node, CLI_COMMAND.format(command=command), timeout=90
        )

    def _apply(self, *lines: str) -> None:
        self.runtime.iosxr_apply_config(self.node, [*lines, "end"])

    def bgp_asn(self) -> int:
        return self.runtime.iosxr_get_bgp_asn_number(self.node)

    def set_bgp_asn(self, asn: int) -> None:
        self.runtime.iosxr_set_bgp_asn(self.node, asn)

    def bgp_networks(self) -> list[str]:
        return self.runtime.iosxr_list_bgp_networks(self.node)

    def withdraw_bgp_network(self, prefix: str) -> None:
        self.runtime.iosxr_withdraw_bgp_prefix(self.node, prefix)

    def _exec(self, command: str) -> str:
        result = self.runtime.exec(self.node, command, timeout=_CONFIG_TIMEOUT_SEC)
        if result.startswith("[TIMEOUT]"):
            raise RuntimeError(
                f"IOS XR configuration operation timed out on {self.node}"
            )
        return result

    def save_config(self) -> str:
        return self._exec("/pkg/bin/xr_cli 'show running-config'")

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
        from nika.net_env.vendor_validation import iosxr_bgp_established_peers

        return iosxr_bgp_established_peers(self._show("show bgp summary"))

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

    def _ebgp_policies(self) -> dict[str, list[str]]:
        """eBGP neighbor -> its IPv4 unicast ``route-policy`` lines."""
        config = self._show("show running-config router bgp")
        local = _ROUTER_BGP.search(config)
        policies = {}
        for peer, body in _NEIGHBOR_BLOCK.findall(config):
            remote = re.search(r"^  remote-as (\d+)$", body, re.MULTILINE)
            unicast = re.search(
                r"^  address-family ipv4 unicast\n(.*?)^  !$", body, re.M | re.S
            )
            if local and remote and remote[1] != local[1]:
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

    # Routes

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
