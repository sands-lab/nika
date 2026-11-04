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


def srl_subif(name: str) -> str:
    """Map a plan, Containerlab, or NOS interface name to an SR Linux subinterface."""
    base, _, index = name.partition(".")
    port = srl_linux_intf(base)
    if port.startswith("e1-"):
        return port.replace("e1-", "ethernet-1/", 1) + "." + (index or "0")
    return name if "." in name else name + ".0"


def srl_json(output: str) -> dict:
    """Decode SR Linux JSON and remove YANG module prefixes from object keys."""
    if not output.lstrip().startswith("{"):
        raise ValueError(f"SR Linux did not return JSON: {output[:200]}")

    def unqualified(value):
        if isinstance(value, dict):
            return {k.split(":")[-1]: unqualified(v) for k, v in value.items()}
        if isinstance(value, list):
            return [unqualified(v) for v in value]
        return value

    return unqualified(json.loads(output))


def srl_instance(output: str, protocol: str) -> dict:
    """Read the single configured protocol instance in network-instance default."""
    data = srl_json(output)
    ni = next(n for n in data["network-instance"] if n["name"] == "default")
    instances = ni["protocols"][protocol]["instance"]
    if len(instances) != 1:
        raise ValueError(f"Expected one SR Linux {protocol} instance")
    return instances[0]


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

    def srl_read(
        self: SupportsSRL, device_name: str, path: str, *, state: bool = True
    ) -> dict:
        """Read a configuration or operational subtree as JSON."""
        datastore = "state" if state else "running"
        return srl_json(
            self.srl_exec_cli(device_name, f"info from {datastore} {path} | as json")
        )

    def srl_protocol_instance(
        self: SupportsSRL, device_name: str, protocol: str, *, state: bool = True
    ) -> dict:
        datastore = "state" if state else "running"
        return srl_instance(
            self.srl_exec_cli(
                device_name,
                f"info from {datastore} network-instance default protocols {protocol} | as json",
            ),
            protocol,
        )

    def srl_configure(self: SupportsSRL, device_name: str, *commands: str) -> None:
        """Commit CLI changes, rejecting an unsuccessful candidate transaction."""
        output = self._srl_candidate(device_name, *commands)
        if "All changes have been committed" not in output or re.search(
            r"error:|parsing error|failed to", output, re.I
        ):
            raise ValueError(f"SR Linux commit failed on {device_name!r}: {output}")

    def srl_routes(self: SupportsSRL, device_name: str, protocol: str) -> set[str]:
        """Read active IPv4 prefixes installed by a protocol in the NOS route table."""
        data = self.srl_read(
            device_name, "network-instance default route-table ipv4-unicast"
        )
        table = data["network-instance"][0]["route-table"]["ipv4-unicast"]
        route_type = "ospfv2" if protocol == "ospf" else protocol
        return {
            row["ipv4-prefix"]
            for row in table.get("route", [])
            if row.get("active")
            and row.get("route-type", "").split(":")[-1].lower() == route_type
        }

    def srl_ospf_neighbors(self: SupportsSRL, device_name: str) -> list[dict]:
        instance = srl_instance(
            self.srl_exec_cli(
                device_name,
                "info from state network-instance default protocols ospf instance * area * interface * | as json",
            ),
            "ospf",
        )
        return [
            {
                "router_id": n["router-id"],
                "address": n["address"],
                "state": n["adjacency-state"].split(":")[-1],
                "iface": srl_linux_intf(interface["interface-name"]),
            }
            for area in instance.get("area", [])
            for interface in area.get("interface", [])
            for n in interface.get("neighbor", [])
        ]

    def srl_isis_neighbors(self: SupportsSRL, device_name: str) -> list[dict]:
        instance = srl_instance(
            self.srl_exec_cli(
                device_name,
                "info from state network-instance default protocols isis instance * interface * | as json",
            ),
            "isis",
        )
        return [
            {
                "system": n["neighbor-system-id"].lower(),
                "state": n["state"],
                "intf": srl_linux_intf(interface["interface-name"]),
            }
            for interface in instance.get("interface", [])
            for n in interface.get("adjacency", [])
        ]

    def srl_bgp_neighbors(self: SupportsSRL, device_name: str) -> dict[str, dict]:
        data = self.srl_read(device_name, "network-instance default protocols bgp")
        bgp = data["network-instance"][0]["protocols"]["bgp"]
        return {
            n["peer-address"]: {"state": n["session-state"], "notification": None}
            for n in bgp.get("neighbor", [])
        }

    def srl_bgp_received_prefixes(
        self: SupportsSRL, device_name: str, peer: str
    ) -> set[str]:
        data = self.srl_read(
            device_name,
            "network-instance default bgp-rib afi-safi ipv4-unicast ipv4-unicast rib-in-out rib-in-post",
        )
        if not data:
            return set()
        rib = data["network-instance"][0]["bgp-rib"]["afi-safi"][0]["ipv4-unicast"][
            "rib-in-out"
        ].get("rib-in-post", {})
        return {r["prefix"] for r in rib.get("route", []) if r["neighbor"] == peer}

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
        bgp_matches = {f"match transport {port} value 179" for port in _BGP_ACL_PORTS}
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
                return dict(zip((first, first + 1), _BGP_ACL_PORTS))
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
