"""SR Linux (Nokia SRL) API for Containerlab labs."""

from __future__ import annotations

import ipaddress
import re

from nika.service.containerlab.protocols import SupportsSRL

NIKA_BGP_ACL = "nika_bgp_block"
NIKA_BGP_WITHDRAW = "nika_bgp_withdraw"
NIKA_BGP_WITHDRAW_PFX = "nika_bgp_withdraw_pfx"
NIKA_BGP_EXPORT_GROUP = "clos01"
NIKA_BLACKHOLE_NHG = "nika_blackhole"
# Transport port fields matched against TCP/179, in drop-entry order.
_NIKA_BGP_ACL_PORTS = ("destination-port", "source-port")
_CPM_IPV4_ENTRY_RE = re.compile(
    r"^set / acl acl-filter cpm type ipv4 entry (\d+) (.+)$", re.MULTILINE
)

# Containerlab maps SRL YANG interfaces to Linux veth names in the netns.
_SRL_SUBIF_TO_LINUX: dict[str, str] = {
    "ethernet-1/1.0": "e1-1",
    "ethernet-1/2.0": "e1-2",
}


def _srl_linux_intf(subinterface: str) -> str:
    if subinterface in _SRL_SUBIF_TO_LINUX:
        return _SRL_SUBIF_TO_LINUX[subinterface]
    if subinterface.startswith("ethernet-1/"):
        return subinterface.replace("ethernet-1/", "e1-").split(".")[0]
    return subinterface.split(".")[0]


class SRLAPIMixin:
    """Interfaces to interact with Nokia SR Linux routers in Containerlab labs."""

    def uses_srl_router(self: SupportsSRL, device_name: str) -> bool:
        """Return True when ``device_name`` runs Nokia SR Linux."""
        if getattr(self, "backend", None) != "containerlab":
            return False
        output = self.exec_cmd(device_name, "command -v sr_cli 2>/dev/null || true")
        return "sr_cli" in output

    def _srl_run_script(
        self: SupportsSRL,
        device_name: str,
        lines: list[str],
        *,
        timeout: float = 30.0,
    ) -> str:
        script = "sr_cli <<'EOF'\n" + "\n".join(lines) + "\nEOF"
        return self.exec_cmd(device_name, script, timeout=timeout)

    def _srl_candidate(
        self: SupportsSRL,
        device_name: str,
        *commands: str,
        timeout: float = 30.0,
    ) -> str:
        return self._srl_run_script(
            device_name,
            ["enter candidate", *commands, "commit now"],
            timeout=timeout,
        )

    def srl_exec_cli(
        self: SupportsSRL,
        device_name: str,
        command: str,
        *,
        timeout: float = 30.0,
    ) -> str:
        """Run a one-shot ``sr_cli`` command on ``device_name``."""
        escaped = command.replace("\\", "\\\\").replace('"', '\\"')
        return self.exec_cmd(device_name, f'sr_cli "{escaped}"', timeout=timeout)

    def srl_get_bgp_as(self: SupportsSRL, device_name: str) -> int:
        output = self.srl_exec_cli(
            device_name,
            "show network-instance default protocols bgp summary",
        )
        match = re.search(r"Global AS number\s+:\s+(\d+)", output)
        if match:
            return int(match.group(1))
        match = re.search(r"autonomous-system\s+:\s+(\d+)", output)
        if match:
            return int(match.group(1))
        raise ValueError(
            f"Could not determine BGP ASN on SRL node {device_name!r}: {output!r}"
        )

    def srl_set_bgp_as(self: SupportsSRL, device_name: str, asn: int) -> None:
        self._srl_candidate(
            device_name,
            f"/network-instance default protocols bgp autonomous-system {asn}",
        )

    def _srl_cpm_ipv4_entries(
        self: SupportsSRL, device_name: str
    ) -> dict[int, list[str]]:
        output = self.srl_exec_cli(
            device_name, "info flat from running acl acl-filter cpm type ipv4"
        )
        entries: dict[int, list[str]] = {}
        for seq, rest in _CPM_IPV4_ENTRY_RE.findall(output):
            entries.setdefault(int(seq), []).append(rest)
        return entries

    def srl_bgp_acl_drop_179_entries(
        self: SupportsSRL, device_name: str
    ) -> dict[int, str]:
        """Choose free CPM filter sequence IDs for the TCP/179 drop entries.

        The drops must precede the lowest entry accepting TCP/179 (the first
        entry when none does); they take the middle of the nearest gap with
        two free IDs below it. Returns sequence ID -> transport port field.
        """
        entries = self._srl_cpm_ipv4_entries(device_name)
        if not entries:
            raise ValueError(f"SRL node {device_name!r} has no CPM IPv4 filter entries")
        bgp_matches = {
            f"match transport {port} value 179" for port in _NIKA_BGP_ACL_PORTS
        }
        bgp_accepts = [
            seq
            for seq, lines in entries.items()
            if not bgp_matches.isdisjoint(lines)
            and any(line.startswith("action accept") for line in lines)
        ]
        anchor = min(bgp_accepts or entries)
        hi = anchor
        for lo in [*sorted((seq for seq in entries if seq < anchor), reverse=True), 0]:
            if hi - lo > 2:
                first = min((lo + hi) // 2, hi - 2)
                return dict(zip((first, first + 1), _NIKA_BGP_ACL_PORTS))
            hi = lo
        raise ValueError(
            f"No two adjacent free CPM IPv4 filter sequence IDs before entry "
            f"{anchor} on SRL node {device_name!r}"
        )

    def srl_add_bgp_acl_drop_179(self: SupportsSRL, device_name: str) -> dict[int, str]:
        """Drop BGP TCP/179 in the CPM filter, then reset established peers.

        BGP runs in the ``srbase-default`` netns, so root-netns iptables rules
        never see it. Returns the drop entries (sequence ID -> transport port
        field), placed by ``srl_bgp_acl_drop_179_entries``.
        """
        drop_entries = self.srl_bgp_acl_drop_179_entries(device_name)
        commands: list[str] = []
        for seq, port in drop_entries.items():
            entry = f"/acl acl-filter cpm type ipv4 entry {seq}"
            commands += [
                f"{entry} description {NIKA_BGP_ACL}",
                f"{entry} match ipv4 protocol tcp",
                f"{entry} match transport {port} operator eq",
                f"{entry} match transport {port} value 179",
                f"{entry} action drop",
            ]
        self._srl_candidate(device_name, *commands)
        # Reset sessions so the drop takes effect now instead of at hold-timer
        # expiry. ``tools`` rejects wildcards, so reset each configured peer.
        neighbors = re.findall(
            r"bgp neighbor (\S+) peer-group",
            self.srl_exec_cli(
                device_name,
                "info flat from running network-instance default protocols bgp "
                "neighbor * peer-group",
            ),
        )
        if neighbors:
            self._srl_run_script(
                device_name,
                [
                    "tools network-instance default protocols bgp "
                    f"neighbor {peer} reset-peer"
                    for peer in neighbors
                ],
            )
        return drop_entries

    def srl_bgp_acl_drop_179_present(
        self: SupportsSRL, device_name: str, drop_entries: dict[int, str]
    ) -> bool:
        """Return True when every ``drop_entries`` entry drops TCP/179."""
        entries = self._srl_cpm_ipv4_entries(device_name)
        return bool(drop_entries) and all(
            f"match transport {port} value 179" in entries.get(seq, [])
            and "action drop" in entries.get(seq, [])
            for seq, port in drop_entries.items()
        )

    def srl_withdraw_client_prefix(
        self: SupportsSRL,
        device_name: str,
        *,
        subinterface: str = "ethernet-1/2.0",
    ) -> None:
        """Withdraw client-facing prefix by bringing down the Linux veth."""
        intf = _srl_linux_intf(subinterface)
        self.exec_cmd(device_name, f"ip link set {intf} down")

    def srl_client_subinterface_disabled(
        self: SupportsSRL, device_name: str, *, subinterface: str
    ) -> bool:
        intf = _srl_linux_intf(subinterface)
        output = self.exec_cmd(
            device_name, f"cat /sys/class/net/{intf}/operstate 2>/dev/null || true"
        )
        return output.strip().lower() == "down"

    def srl_add_blackhole_static(
        self: SupportsSRL, device_name: str, prefix: str
    ) -> None:
        """Install a dataplane blackhole static route in network-instance default.

        Linux ``ip route blackhole`` only affects the SRL host netns and does not
        drop traffic forwarded by the NOS FIB; use an SRL next-hop-group instead.
        """
        prefix_str = str(ipaddress.ip_network(prefix, strict=False))
        self._srl_candidate(
            device_name,
            f"/network-instance default next-hop-groups group {NIKA_BLACKHOLE_NHG} "
            "admin-state enable",
            f"/network-instance default next-hop-groups group {NIKA_BLACKHOLE_NHG} "
            "blackhole",
            f"/network-instance default static-routes route {prefix_str} "
            f"next-hop-group {NIKA_BLACKHOLE_NHG}",
            f"/network-instance default static-routes route {prefix_str} "
            "admin-state enable",
            # Prefer over BGP (default preference 170) so the discard wins.
            f"/network-instance default static-routes route {prefix_str} preference 1",
        )

    def srl_blackhole_static_present(
        self: SupportsSRL, device_name: str, prefix: str
    ) -> bool:
        prefix_str = str(ipaddress.ip_network(prefix, strict=False))
        routes = self.srl_exec_cli(
            device_name, "info from running network-instance default static-routes"
        )
        nhg = self.srl_exec_cli(
            device_name,
            "info from running network-instance default next-hop-groups "
            f"group {NIKA_BLACKHOLE_NHG}",
        )
        return prefix_str in routes and "blackhole" in nhg.lower()

    def srl_advertise_prefix(self: SupportsSRL, device_name: str, prefix: str) -> None:
        """Advertise ``prefix`` by attaching it to a dedicated loopback interface."""
        network = ipaddress.ip_network(prefix, strict=False)
        if network.prefixlen == 31:
            hosts = list(network.hosts())
            host_addr = hosts[-1] if hosts else network.network_address + 1
            lo_addr = f"{host_addr}/{network.prefixlen}"
        else:
            host_addr = next(network.hosts(), network.network_address + 1)
            lo_addr = f"{host_addr}/{network.prefixlen}"

        self._srl_candidate(
            device_name,
            "/interface lo1 admin-state enable",
            f"/interface lo1 subinterface 0 ipv4 address {lo_addr}",
            "/network-instance default interface lo1.0",
        )

    def srl_prefix_advertised(self: SupportsSRL, device_name: str, prefix: str) -> bool:
        output = self.srl_exec_cli(
            device_name,
            f"show network-instance default protocols bgp routes ipv4 prefix {prefix}",
        )
        prefix_base = prefix.split("/")[0]
        return prefix_base in output or prefix in output

    def srl_add_blackhole_route_leak(
        self: SupportsSRL, device_name: str, prefix: str
    ) -> None:
        self.srl_add_blackhole_static(device_name, prefix)

    def srl_withdraw_bgp_prefix(
        self: SupportsSRL, device_name: str, prefix: str
    ) -> None:
        """Stop exporting ``prefix`` to BGP peers via an export routing-policy."""
        network = ipaddress.ip_network(prefix, strict=False)
        prefix_str = str(network)
        self._srl_candidate(
            device_name,
            f"/routing-policy prefix-set {NIKA_BGP_WITHDRAW_PFX} prefix {prefix_str} mask-length-range exact",
            f"/routing-policy policy {NIKA_BGP_WITHDRAW} default-action policy-result accept",
            f"/routing-policy policy {NIKA_BGP_WITHDRAW} statement 10 match prefix-set {NIKA_BGP_WITHDRAW_PFX}",
            f"/routing-policy policy {NIKA_BGP_WITHDRAW} statement 10 action policy-result reject",
            f"/network-instance default protocols bgp group {NIKA_BGP_EXPORT_GROUP} export-policy [{NIKA_BGP_WITHDRAW}]",
        )

    def srl_bgp_prefix_withdrawn(
        self: SupportsSRL, device_name: str, prefix: str
    ) -> bool:
        """Return True when export-policy blocks ``prefix`` from BGP export."""
        network = ipaddress.ip_network(prefix, strict=False)
        prefix_str = str(network)
        policy_output = self.srl_exec_cli(
            device_name, "info from running routing-policy"
        )
        bgp_output = self.srl_exec_cli(
            device_name, "info from running network-instance default protocols bgp"
        )
        return (
            NIKA_BGP_WITHDRAW in policy_output
            and prefix_str in policy_output
            and NIKA_BGP_WITHDRAW in bgp_output
        )
