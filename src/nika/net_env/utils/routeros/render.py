"""Render router intents as RouterOS 7 auto-import scripts.

Callers pass RouterOS port names (``ether2``…), already mapped from the
container interfaces of the router's attachment layout.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from ipaddress import IPv4Interface, IPv4Network


def render_base(name: str, addresses: Iterable[tuple[str, str]]) -> list[str]:
    """Set the identity and ``(RouterOS port, CIDR)`` addresses."""
    return [f"/system identity set name={name}"] + [
        f"/ip address add address={IPv4Interface(cidr)} interface={port}"
        for port, cidr in addresses
    ]


def render_ospf(
    router_id: str,
    networks: Mapping[str, str],
    *,
    costs: Mapping[str, int] | None = None,
) -> list[str]:
    """Render OSPFv2 with ``network -> area`` statements, like FRR ``network``.

    ``costs`` sets the interface cost of the template for a network.
    """
    costs = costs or {}
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
            f"/routing ospf interface-template add area=area-{area} "
            f"networks={network}"
            + (f" cost={costs[network]}" if network in costs else "")
        )
    return lines


def render_ebgp(
    asn: int,
    router_id: str,
    peers: Iterable[tuple[str, int]],
    *,
    networks: Iterable[str] = (),
    filters: Mapping[tuple[str, str], str] | None = None,
    chains: Mapping[str, Sequence[tuple[str, bool]]] | None = None,
) -> list[str]:
    """Render BGP sessions that originate ``networks``.

    Like FRR with ``no bgp ebgp-requires-policy``, a session without a filter
    accepts and re-advertises every route. ``filters`` binds a
    ``(peer, "in" | "out")`` to a chain in ``chains``; a chain applies its
    first exact-prefix match and rejects every unmatched route, like an FRR
    route map over exact prefix lists. Each network must be a connected prefix.
    """
    networks = sorted(networks)
    filters, chains = filters or {}, chains or {}
    lines = [
        f"/ip firewall address-list add list=BGP-NETWORKS address={network}"
        for network in networks
    ]
    for chain in sorted(set(filters.values())):
        for prefix, permit in chains[chain]:
            network = IPv4Network(prefix)
            lines.append(
                f"/routing filter rule add chain={chain} "
                f'rule="if (dst == {network} && dst-len == {network.prefixlen}) '
                f'{{ {"accept" if permit else "reject"}; }}"'
            )
        lines.append(f'/routing filter rule add chain={chain} rule="reject;"')
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
        if chain := filters.get((address, "in")):
            command += f" input.filter={chain}"
        if chain := filters.get((address, "out")):
            command += f" output.filter-chain={chain}"
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
