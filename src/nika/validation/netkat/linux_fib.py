"""Model the forwarding state that Linux routers installed as NetKAT rules.

Routed environments forward with the kernel FIB of each router. This adapter
reads ``ip -j -4 addr`` and ``ip -j -4 route`` from every node and builds a
:class:`ForwardingNetwork` for the routers, with the declared hosts as
endpoints, so that NetKAT checks reachability on the forwarding state a
running network actually uses.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv4Interface, IPv4Network

from nika.validation.netkat.model import (
    ForwardingAction,
    ForwardingEndpoint,
    ForwardingNetwork,
    ForwardingRule,
)

ASSUMPTIONS = (
    "The main IPv4 routing table of each router; VRF tables, policy routing, "
    "packet filters, NAT and tunnels are not modeled.",
    "Neighbor resolution succeeds for every next hop and connected address.",
    "Each host forwards through the router that owns its default gateway.",
)

_DROP = {"blackhole", "unreachable", "prohibit"}


def _mac(node: str) -> str:
    """Stable synthetic MAC of a node; forwarding is decided by IPv4 prefixes."""
    value = sum(ord(c) * 131**i for i, c in enumerate(node)) % (1 << 40)
    return "02:" + ":".join(f"{(value >> s) & 0xFF:02x}" for s in range(32, -1, -8))


def addresses(addr_json: list) -> list[IPv4Interface]:
    return [
        IPv4Interface(f"{a['local']}/{a['prefixlen']}")
        for link in addr_json
        for a in link.get("addr_info", [])
        if a.get("family") == "inet" and not a["local"].startswith("127.")
    ]


def fib_network(
    state: dict,
    routers: set[str],
    hosts: dict[str, str],
    local: dict[str, str] | None = None,
) -> ForwardingNetwork:
    """Build the forwarding network of ``routers`` with ``hosts`` as endpoints.

    ``state`` maps each node to ``{"addr": [...], "route": [...]}`` as ``ip -j``
    prints them; ``hosts`` maps endpoint names to their IPv4 addresses.
    ``local`` maps endpoints that stand for a router's own traffic to that
    router: they attach to it, and the router delivers its own addresses to
    them.
    """
    local = local or {}
    owner = {}
    for node, data in state.items():
        for interface in addresses(data["addr"]):
            owner.setdefault(interface.ip, node)
    endpoints = {}
    for host, address in hosts.items():
        if host in local:
            endpoints[host] = ForwardingEndpoint(
                address=address, switch=local[host], mac=_mac(host)
            )
            continue
        gateways = [
            r["gateway"]
            for r in state[host]["route"]
            if r.get("dst") == "default" and r.get("gateway")
        ]
        attached = {owner.get(IPv4Address(g)) for g in gateways} & routers
        if len(attached) != 1:
            raise ValueError(f"Host {host} needs one default gateway router")
        endpoints[host] = ForwardingEndpoint(
            address=address, switch=attached.pop(), mac=_mac(host)
        )

    modeled = routers | set(endpoints)

    def action(peer: str) -> ForwardingAction:
        return ForwardingAction(peer=peer, destination_mac=_mac(peer))

    rules = {}
    for router in sorted(routers):
        best = {}
        for route in state[router]["route"]:
            prefix = IPv4Network(
                "0.0.0.0/0" if route["dst"] == "default" else route["dst"], strict=False
            )
            metric = route.get("metric", 0)
            if prefix in best and best[prefix][0] <= metric:
                continue
            best[prefix] = (metric, route)
        rows = []
        for prefix, (_, route) in best.items():
            if route.get("type") in _DROP:
                rows.append(
                    ForwardingRule(
                        prefix=str(prefix), priority=prefix.prefixlen * 2, actions=()
                    )
                )
                continue
            hops = [h["gateway"] for h in route.get("nexthops", []) if h.get("gateway")]
            if route.get("gateway"):
                hops.append(route["gateway"])
            peers = sorted({owner.get(IPv4Address(h)) for h in hops} & modeled)
            if peers:
                rows.append(
                    ForwardingRule(
                        prefix=str(prefix),
                        priority=prefix.prefixlen * 2,
                        actions=tuple(action(p) for p in peers),
                    )
                )
                continue
            if route.get("scope") != "link":
                continue
            # A connected subnet delivers to the node that owns each address.
            for address, node in sorted(owner.items()):
                if (
                    address in prefix
                    and node in modeled
                    and node != router
                    and prefix.prefixlen < 32
                    and IPv4Network(f"{address}/32") not in best
                ):
                    rows.append(
                        ForwardingRule(
                            prefix=f"{address}/32",
                            priority=65,
                            actions=(action(node),),
                        )
                    )
        for endpoint, owner_router in local.items():
            if owner_router != router:
                continue
            rows += [
                ForwardingRule(
                    prefix=f"{interface.ip}/32",
                    priority=66,
                    actions=(action(endpoint),),
                )
                for interface in addresses(state[router]["addr"])
            ]
        rules[router] = tuple(rows)
    return ForwardingNetwork(endpoints=endpoints, rules=rules, assumptions=ASSUMPTIONS)
