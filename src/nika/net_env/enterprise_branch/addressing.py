"""Address helpers for enterprise_branch."""

from __future__ import annotations

from ipaddress import IPv4Interface, IPv4Network

ROLE_OCTET = {
    "corp": 10,
    "server": 20,
    "iot": 30,
    "guest": 40,
}

# Linux VRF table ids (stable across sizes).
VRF_TABLE = {
    "corp": 10,
    "server": 20,
    "iot": 30,
    "guest": 40,
}


def vrf_name(role: str) -> str:
    """Linux VRF device name for a business role."""
    return f"vrf_{role}"


def lan_network(site_id: int, role: str) -> IPv4Network:
    return IPv4Network(f"10.{site_id}.{ROLE_OCTET[role]}.0/24")


def lan_edge_ip(site_id: int, role: str) -> IPv4Interface:
    net = lan_network(site_id, role)
    return IPv4Interface(f"{net.network_address + 1}/{net.prefixlen}")


def lan_host_ip(site_id: int, role: str, host_index: int = 0) -> IPv4Interface:
    """Host address on a LAN; index 0 is .2 (legacy primary endpoint)."""
    if host_index < 0:
        raise ValueError("host_index must be >= 0")
    net = lan_network(site_id, role)
    # .1 is the Site Edge; hosts start at .2
    host = net.network_address + 2 + host_index
    if host not in net:
        raise ValueError(f"host_index {host_index} overflows {net} for role {role!r}")
    return IPv4Interface(f"{host}/{net.prefixlen}")


def assign_p2p_ips(subnet: IPv4Network) -> tuple[str, str]:
    base = subnet.network_address
    ip0 = IPv4Interface(f"{base}/30")
    ip1 = IPv4Interface(f"{base + 1}/30")
    return str(ip0), str(ip1)


def host_name_for(site_name: str, role: str, host_index: int = 0) -> str:
    if host_index < 0:
        raise ValueError("host_index must be >= 0")
    if role == "server":
        base = f"{site_name}_srv"
    elif role == "guest":
        base = f"{site_name}_guest_pc"
    elif role == "iot":
        base = f"{site_name}_iot_pc"
    else:
        base = f"{site_name}_corp_pc"
    if host_index == 0:
        return base
    return f"{base}{host_index + 1}"


def edge_name_for(site_name: str) -> str:
    return f"{site_name}_edge"


def isp_name_for(provider: str) -> str:
    return f"{provider}_core"
