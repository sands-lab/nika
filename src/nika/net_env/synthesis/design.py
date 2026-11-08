"""Routing design that an environment records while it builds its lab.

Configuration generators such as netlab compile this record instead of reading
configuration text that NIKA rendered from the same design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface, IPv4Network


@dataclass(frozen=True)
class WireGuardTunnel:
    """WireGuard endpoint of a point-to-point tunnel interface."""

    # Underlay interface whose address is the tunnel endpoint.
    source: str
    private_key: str
    public_key: str
    listen_port: int
    allowed_ips: str = "0.0.0.0/0"
    keepalive: int = 25
    mtu: int = 1420


@dataclass
class DesignInterface:
    name: str
    address: str
    # Lab link shared with the peer interfaces; None for a router-owned
    # segment, such as an access bridge, that NIKA keeps outside the sketch.
    link: str | None
    vrf: str | None = None
    wireguard: WireGuardTunnel | None = None


@dataclass(frozen=True)
class PrefixEntry:
    prefix: str
    permit: bool = True
    le: int | None = None


@dataclass(frozen=True)
class PolicyEntry:
    permit: bool = True
    match_prefix_list: str | None = None
    local_preference: int | None = None


@dataclass
class DesignNeighbor:
    address: str
    remote_as: int
    # Interface carrying the session when it is not a lab link, such as a tunnel.
    interface: str | None = None
    import_policy: str | None = None
    export_policy: str | None = None


@dataclass(frozen=True)
class RetainedStatements:
    """Configuration NIKA renders because the generator cannot express it."""

    capability: str
    # Generator limitation that requires NIKA to render this capability.
    reason: str
    text: str


@dataclass
class DesignRouter:
    name: str
    router_id: str = ""
    interfaces: list[DesignInterface] = field(default_factory=list)
    asn: int | None = None
    bgp_networks: list[str] = field(default_factory=list)
    # False when external BGP routes are accepted and sent without a policy.
    ebgp_requires_policy: bool = True
    # Equal-cost eBGP paths, with AS paths of the same length treated as equal.
    bgp_multipath: int | None = None
    # FRR debug statements, without the leading ``debug``, logged to the FRR log.
    debug: list[str] = field(default_factory=list)
    neighbors: list[DesignNeighbor] = field(default_factory=list)
    # OSPF network statements: (prefix, dotted area).
    ospf_networks: list[tuple[str, str]] = field(default_factory=list)
    prefix_lists: dict[str, list[PrefixEntry]] = field(default_factory=dict)
    policies: dict[str, list[PolicyEntry]] = field(default_factory=dict)
    # Static routes as (prefix, next hop), installed by the router's Linux startup.
    static_routes: list[tuple[str, str]] = field(default_factory=list)
    retained: list[RetainedStatements] = field(default_factory=list)

    def add_interface(
        self,
        name: str,
        address: str,
        link: str | None,
        *,
        vrf: str | None = None,
        wireguard: WireGuardTunnel | None = None,
    ) -> str:
        """Record an addressed interface and return its Linux startup command."""
        IPv4Interface(address)
        self.interfaces.append(DesignInterface(name, address, link, vrf, wireguard))
        return f"ip addr add {address} dev {name}"

    def interface(self, name: str) -> DesignInterface:
        (found,) = (i for i in self.interfaces if i.name == name)
        return found

    def add_static_route(self, prefix: str, via: str) -> str:
        """Record a static route and return its Linux startup command."""
        IPv4Network(prefix)
        IPv4Address(via)
        self.static_routes.append((prefix, via))
        return f"ip route add {prefix} via {via}"

    def add_ospf_network(self, prefix, area) -> str:
        """Record an OSPF network statement and return its FRR line."""
        self.ospf_networks.append((str(IPv4Network(prefix)), _area(area)))
        return f"network {prefix} area {area}"

    def ospf_area(self, interface: DesignInterface) -> str | None:
        """Return the single OSPF area whose network contains the interface."""
        ip = IPv4Interface(interface.address).ip
        areas = {a for prefix, a in self.ospf_networks if ip in IPv4Network(prefix)}
        if len(areas) > 1:
            raise ValueError(f"{self.name}:{interface.name} has two OSPF areas")
        return areas.pop() if areas else None

    def add_neighbor(self, address: str, remote_as: int, **kwargs) -> None:
        IPv4Address(address)
        self.neighbors.append(DesignNeighbor(address, remote_as, **kwargs))

    @property
    def runs_ospf(self) -> bool:
        return bool(self.ospf_networks)


class RoutingDesign:
    """Routers in lab order, each with its addressing and protocol design."""

    def __init__(self) -> None:
        self.routers: dict[str, DesignRouter] = {}

    def router(self, name: str) -> DesignRouter:
        return self.routers.setdefault(name, DesignRouter(name))

    def origin_prefixes(self) -> dict[str, str]:
        """Return the router that originates each BGP network."""
        origins = {}
        for router in self.routers.values():
            for prefix in router.bgp_networks:
                if prefix in origins:
                    raise ValueError("The BGP sketch requires one origin per prefix")
                origins[prefix] = router.name
        return origins


def _area(value) -> str:
    """Normalize an OSPF area to dotted-quad notation."""
    text = str(value)
    return str(IPv4Address(int(text) if "." not in text else text))
