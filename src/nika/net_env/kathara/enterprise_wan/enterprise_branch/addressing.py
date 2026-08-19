"""Address helpers for enterprise_branch."""

from __future__ import annotations

from ipaddress import IPv4Interface, IPv4Network

ROLE_OCTET = {
    "corp": 10,
    "server": 20,
    "guest": 40,
}


def lan_network(site_id: int, role: str) -> IPv4Network:
    return IPv4Network(f"10.{site_id}.{ROLE_OCTET[role]}.0/24")


def lan_edge_ip(site_id: int, role: str) -> IPv4Interface:
    net = lan_network(site_id, role)
    return IPv4Interface(f"{net.network_address + 1}/{net.prefixlen}")


def lan_host_ip(site_id: int, role: str) -> IPv4Interface:
    net = lan_network(site_id, role)
    return IPv4Interface(f"{net.network_address + 2}/{net.prefixlen}")


def assign_p2p_ips(subnet: IPv4Network) -> tuple[str, str]:
    base = subnet.network_address
    ip0 = IPv4Interface(f"{base}/30")
    ip1 = IPv4Interface(f"{base + 1}/30")
    return str(ip0), str(ip1)


def host_name_for(site_name: str, role: str) -> str:
    if role == "server":
        return f"{site_name}_srv"
    if role == "guest":
        return f"{site_name}_guest_pc"
    return f"{site_name}_corp_pc"


def edge_name_for(site_name: str) -> str:
    return f"{site_name}_edge"


def isp_name_for(provider: str) -> str:
    return f"{provider}_core"
