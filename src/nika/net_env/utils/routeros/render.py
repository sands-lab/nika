"""Render router intents as RouterOS 7 auto-import scripts.

Callers pass RouterOS port names (``ether2``…), already mapped from the
container interfaces of the router's attachment layout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from ipaddress import IPv4Interface


def render_base(name: str, addresses: Iterable[tuple[str, str]]) -> list[str]:
    """Set the identity and ``(RouterOS port, CIDR)`` addresses."""
    return [f"/system identity set name={name}"] + [
        f"/ip address add address={IPv4Interface(cidr)} interface={port}"
        for port, cidr in addresses
    ]


def render_ospf(router_id: str, networks: Mapping[str, str]) -> list[str]:
    """Render OSPFv2 with ``network -> area`` statements, like FRR ``network``."""
    areas = sorted(
        set(networks.values()), key=lambda area: tuple(map(int, area.split(".")))
    )
    lines = [f"/routing ospf instance add name=CORE version=2 router-id={router_id}"]
    lines.extend(
        f"/routing ospf area add name=area-{area} instance=CORE area-id={area}"
        for area in areas
    )
    for network, area in sorted(networks.items()):
        lines.append(
            f"/routing ospf interface-template add area=area-{area} networks={network}"
        )
    return lines


def render_ebgp(
    asn: int,
    router_id: str,
    peers: Iterable[tuple[str, int]],
    *,
    networks: Iterable[str] = (),
) -> list[str]:
    """Render unfiltered BGP sessions that originate ``networks``.

    Like FRR with ``no bgp ebgp-requires-policy``, sessions accept and
    re-advertise every route. Each network must be a connected prefix.
    """
    networks = sorted(networks)
    lines = [
        f"/ip firewall address-list add list=BGP-NETWORKS address={network}"
        for network in networks
    ]
    lines.append(f"/routing bgp instance add name=CORE as={asn} router-id={router_id}")
    for index, (address, remote_asn) in enumerate(sorted(peers)):
        command = (
            f"/routing bgp connection add name=peer-{index} instance=CORE "
            f"local.role={'ibgp' if remote_asn == asn else 'ebgp'} "
            f"remote.address={address} remote.as={remote_asn} "
            "afi=ip connect=yes listen=yes"
        )
        if networks:
            command += " output.network=BGP-NETWORKS"
        lines.append(command)
    return lines


def render_script(*sections: Iterable[str]) -> str:
    return "\n".join(line for section in sections for line in section) + "\n"


def render_bridge(ports: Iterable[str], addresses: Iterable[str]) -> list[str]:
    """Bridge RouterOS ``ports`` as ``br0`` and assign its gateway addresses."""
    lines = ["/interface bridge add name=br0 protocol-mode=none"]
    lines.extend(
        f"/interface bridge port add bridge=br0 interface={port}" for port in ports
    )
    lines.extend(
        f"/ip address add address={IPv4Interface(cidr)} interface=br0"
        for cidr in addresses
    )
    return lines


def render_dhcp_relay(server: str, gateway: str) -> list[str]:
    """Relay ``br0`` DHCP requests to ``server`` with the gateway as giaddr."""
    return [
        "/ip dhcp-relay add name=relay-br0 interface=br0 "
        f"dhcp-server={server} local-address={IPv4Interface(gateway).ip} disabled=no"
    ]
