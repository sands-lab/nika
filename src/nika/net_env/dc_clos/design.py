"""Fixed eBGP bindings for the shared Clos design."""

from dataclasses import dataclass
from ipaddress import IPv4Network

from nika.net_env.clos.design import ClosFabricModel


def router_asns(model: ClosFabricModel) -> dict[str, int]:
    return {
        name: 65001 + index for index, name in enumerate(model.spines + model.leaves)
    }


@dataclass(frozen=True)
class RoutingLink:
    leaf: str
    spine: str
    leaf_address: str
    spine_address: str


def routing_links(model: ClosFabricModel) -> tuple[RoutingLink, ...]:
    subnets = IPv4Network("172.16.0.0/16").subnets(new_prefix=31)
    return tuple(
        RoutingLink(
            leaf, spine, str(subnet.network_address), str(subnet.network_address + 1)
        )
        for (leaf, spine), subnet in zip(model.leaf_spine_links, subnets)
    )
