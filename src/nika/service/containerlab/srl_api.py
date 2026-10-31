"""SR Linux (Nokia SRL) API for Containerlab labs."""

from __future__ import annotations

import ipaddress
import json
import re

from nika.net_env.isp.igp.ifaces import srl_e1_name
from nika.service.containerlab.protocols import SupportsSRL

BGP_ACL_DESCRIPTION = "cpm-protect"
BGP_EXPORT_POLICY = "CLOS01-OUT"
BGP_EXPORT_PREFIX_SET = "CLOS01-OUT-PFX"
STATIC_NHG = "static-nhg-1"
_ETH_RE = re.compile(r"^eth\d+$")
# Transport port fields matched against TCP/179, in drop-entry order.
_BGP_ACL_PORTS = ("destination-port", "source-port")
_CPM_IPV4_ENTRY_RE = re.compile(
    r"^set / acl acl-filter cpm type ipv4 entry (\d+) (.+)$", re.MULTILINE
)
# OSPF is IP protocol 89; the default CPM filter accepts it by number.
_OSPF_ACL_MATCHES = {"match ipv4 protocol 89", "match ipv4 protocol ospf"}
_SRL_OSPF_INTERFACE_RE = re.compile(
    r"^(?:set )?/ network-instance default protocols ospf instance (\S+) area (\S+) "
    r"interface (\S+)\s*(.*)$",
    re.MULTILINE,
)


def _is_data(subinterface: str) -> bool:
    return subinterface.startswith("ethernet-")


# Containerlab maps SRL YANG interfaces to Linux veth names in the netns.
_SRL_SUBIF_TO_LINUX: dict[str, str] = {
    "ethernet-1/1.0": "e1-1",
    "ethernet-1/2.0": "e1-2",
}


def srl_linux_intf(name: str) -> str:
    """Linux port device in an SRL container's default netns for *name*.

    Accepts SRL names (``ethernet-1/2``, ``ethernet-1/2.0``), Containerlab
    endpoint names (``e1-2``, ``e1-2.0``), and NIKA plan names (``eth1``);
    all map to ``e1-2``. Other names (``mgmt0``, ``any``) pass through.
    """
    name = name.strip()
    if name in _SRL_SUBIF_TO_LINUX:
        return _SRL_SUBIF_TO_LINUX[name]
    if name.startswith("ethernet-1/"):
        return name.replace("ethernet-1/", "e1-").split(".")[0]
    if _ETH_RE.match(name):
        return srl_e1_name(name)
    return name.split(".")[0]


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

    def _srl_cpm_drop_sequences(
        self: SupportsSRL, device_name: str, matches: set[str], count: int
    ) -> list[int]:
        """Choose ``count`` adjacent free CPM IPv4 filter sequence IDs.

        The drops must precede the lowest entry that accepts traffic with one
        of ``matches`` (the first entry when none does); they take the middle
        of the nearest gap with ``count`` free IDs below it.
        """
        entries = self._srl_cpm_ipv4_entries(device_name)
        if not entries:
            raise ValueError(f"SRL node {device_name!r} has no CPM IPv4 filter entries")
        accepts = [
            seq
            for seq, lines in entries.items()
            if not matches.isdisjoint(lines)
            and any(line.startswith("action accept") for line in lines)
        ]
        anchor = min(accepts or entries)
        hi = anchor
        for lo in [*sorted((seq for seq in entries if seq < anchor), reverse=True), 0]:
            if hi - lo > count:
                first = min((lo + hi) // 2, hi - count)
                return list(range(first, first + count))
            hi = lo
        raise ValueError(
            f"No {count} adjacent free CPM IPv4 filter sequence IDs before entry "
            f"{anchor} on SRL node {device_name!r}"
        )

    def srl_bgp_acl_drop_179_entries(
        self: SupportsSRL, device_name: str
    ) -> dict[int, str]:
        """Choose free CPM filter sequence IDs for the TCP/179 drop entries.

        Returns sequence ID -> transport port field.
        """
        bgp_matches = {f"match transport {port} value 179" for port in _BGP_ACL_PORTS}
        sequences = self._srl_cpm_drop_sequences(
            device_name, bgp_matches, len(_BGP_ACL_PORTS)
        )
        return dict(zip(sequences, _BGP_ACL_PORTS))

    def srl_add_ospf_acl_drop(self: SupportsSRL, device_name: str) -> list[int]:
        """Drop OSPF (IP protocol 89) in the CPM filter ahead of its accept entry.

        OSPF runs in the ``srbase-default`` netns, so root-netns iptables
        rules never see it. Returns the drop entry's sequence ID.
        """
        sequences = self._srl_cpm_drop_sequences(device_name, _OSPF_ACL_MATCHES, 1)
        entry = f"/acl acl-filter cpm type ipv4 entry {sequences[0]}"
        self._srl_candidate(
            device_name,
            f"{entry} description {BGP_ACL_DESCRIPTION}",
            f"{entry} match ipv4 protocol 89",
            f"{entry} action drop",
        )
        # Reset adjacencies so the drop takes effect now instead of at
        # dead-interval expiry. ``tools`` rejects wildcards, so clear each
        # OSPF data interface.
        commands: list[str] = []
        seen: set[tuple[str, str, str]] = set()
        for instance, area, iface, _rest in self._srl_ospf_flat(
            device_name, "running", ""
        ):
            if not _is_data(iface):
                continue
            key = (instance, area, iface)
            if key in seen:
                continue
            seen.add(key)
            commands.append(
                "tools network-instance default protocols ospf "
                f"instance {instance} area {area} interface {iface} neighbors clear"
            )
        if commands:
            self._srl_run_script(device_name, commands)
        return sequences

    def srl_ospf_acl_drop_present(
        self: SupportsSRL, device_name: str, sequences: list[int]
    ) -> bool:
        """Return True when every entry in ``sequences`` drops OSPF."""
        entries = self._srl_cpm_ipv4_entries(device_name)
        return bool(sequences) and all(
            not _OSPF_ACL_MATCHES.isdisjoint(entries.get(seq, []))
            and "action drop" in entries.get(seq, [])
            for seq in sequences
        )

    def _srl_ospf_flat(self: SupportsSRL, device_name: str, source: str, leaf: str):
        output = self.srl_exec_cli(
            device_name,
            f"info flat from {source} network-instance default protocols ospf "
            f"instance * area * interface * {leaf}",
        )
        return _SRL_OSPF_INTERFACE_RE.findall(output)

    def srl_ospf_interfaces(self: SupportsSRL, device_name: str) -> list[str]:
        """Return the data subinterfaces OSPF is configured on."""
        rows = self._srl_ospf_flat(device_name, "running", "")
        return sorted(
            {iface for _instance, _area, iface, _rest in rows if _is_data(iface)}
        )

    def srl_ospf_neighbor_interfaces(self: SupportsSRL, device_name: str) -> list[str]:
        """Return the subinterface of each Full OSPF adjacency, one per neighbor."""
        rows = self._srl_ospf_flat(device_name, "state", "neighbor * adjacency-state")
        return sorted(
            iface
            for _instance, _area, iface, rest in rows
            if rest.split()[-1:] == ["full"] and _is_data(iface)
        )

    def srl_remove_ospf_interface(
        self: SupportsSRL, device_name: str, interface: str
    ) -> None:
        """Delete ``interface`` from its OSPF area."""
        for instance, area, iface, _rest in self._srl_ospf_flat(
            device_name, "running", ""
        ):
            if iface == interface:
                self._srl_candidate(
                    device_name,
                    f"delete /network-instance default protocols ospf instance "
                    f"{instance} area {area} interface {iface}",
                )
                return
        raise ValueError(f"OSPF does not run on {device_name}:{interface}")

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
                f"{entry} description {BGP_ACL_DESCRIPTION}",
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
        intf = srl_linux_intf(subinterface)
        self.exec_cmd(device_name, f"ip link set {intf} down")

    def srl_client_subinterface_disabled(
        self: SupportsSRL, device_name: str, *, subinterface: str
    ) -> bool:
        intf = srl_linux_intf(subinterface)
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
            f"/network-instance default next-hop-groups group {STATIC_NHG} "
            "admin-state enable",
            f"/network-instance default next-hop-groups group {STATIC_NHG} blackhole",
            f"/network-instance default static-routes route {prefix_str} "
            f"next-hop-group {STATIC_NHG}",
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
            f"group {STATIC_NHG}",
        )
        return prefix_str in routes and "blackhole" in nhg.lower()

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
        state = json.loads(
            self.srl_exec_cli(
                device_name,
                "info from state network-instance default protocols bgp | as json",
            )
        )
        bgp = state["network-instance"][0]["protocols"]["bgp"]
        commands = [
            f"/routing-policy prefix-set {BGP_EXPORT_PREFIX_SET} prefix {prefix_str} mask-length-range exact",
            f"/routing-policy policy {BGP_EXPORT_POLICY} default-action policy-result next-policy",
            f"/routing-policy policy {BGP_EXPORT_POLICY} statement 10 match prefix-set {BGP_EXPORT_PREFIX_SET}",
            f"/routing-policy policy {BGP_EXPORT_POLICY} statement 10 action policy-result reject",
        ]
        if not bgp.get("neighbor"):
            raise ValueError("BGP withdrawal requires actual configured peers")
        for neighbor in bgp["neighbor"]:
            family = next(
                af
                for af in neighbor["afi-safi"]
                if af["afi-safi-name"] == "ipv4-unicast"
            )
            policies = [BGP_EXPORT_POLICY, *family.get("export-policy", [])]
            commands.append(
                f"/network-instance default protocols bgp neighbor {neighbor['peer-address']} afi-safi ipv4-unicast export-policy [{' '.join(policies)}]"
            )
        self._srl_candidate(device_name, *commands)

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
            BGP_EXPORT_POLICY in policy_output
            and prefix_str in policy_output
            and BGP_EXPORT_POLICY in bgp_output
        )
