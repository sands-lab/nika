"""SR Linux (Nokia SRL) API for Containerlab labs."""

from __future__ import annotations

import ipaddress
import re

from nika.service.containerlab.protocols import SupportsSRL

NIKA_BGP_ACL = "nika_bgp_block"
NIKA_BGP_WITHDRAW = "nika_bgp_withdraw"
NIKA_BGP_WITHDRAW_PFX = "nika_bgp_withdraw_pfx"
NIKA_BGP_EXPORT_GROUP = "clos01"

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

    def srl_add_bgp_acl_drop_179(self: SupportsSRL, device_name: str) -> None:
        """Block BGP TCP/179 in the SRL Linux netns."""
        self.exec_cmd(device_name, "iptables -A INPUT -p tcp --dport 179 -j DROP")
        self.exec_cmd(device_name, "iptables -A INPUT -p tcp --sport 179 -j DROP")

    def srl_bgp_acl_drop_179_present(self: SupportsSRL, device_name: str) -> bool:
        output = self.exec_cmd(device_name, "iptables -L INPUT -n 2>/dev/null || true")
        return "dpt:179" in output and "DROP" in output

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
        self.exec_cmd(device_name, f"ip route replace blackhole {prefix}")

    def srl_blackhole_static_present(
        self: SupportsSRL, device_name: str, prefix: str
    ) -> bool:
        output = self.exec_cmd(device_name, "ip route show")
        prefix_base = prefix.split("/")[0]
        return prefix_base in output and "blackhole" in output.lower()

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
