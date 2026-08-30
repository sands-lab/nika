"""Generate FRR configuration for ISP routers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from nika.net_env.isp.igp.plan import PlannedInterface, PlannedNode


def isis_net_from_router_id(router_id: str) -> str:
    """Build an IS-IS NET from an IPv4 router-id (49.0001.<sysid>.00)."""
    octets = [int(part) for part in router_id.split(".")]
    if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        raise ValueError(f"Invalid router-id for IS-IS NET: {router_id!r}")
    # Pad to 6 system-id octets: 00 00 + four IPv4 octets.
    sys = [0, 0, *octets]
    body = "".join(f"{b:02x}" for b in sys)
    grouped = ".".join(body[i : i + 4] for i in range(0, 12, 4))
    return f"49.0001.{grouped}.00"


def render_frr_conf(
    node: PlannedNode,
    *,
    igp: str,
    interfaces: tuple[PlannedInterface, ...],
) -> str:
    if igp == "isis":
        return _render_isis(node, interfaces)
    if igp == "ospf":
        return _render_ospf(node, interfaces)
    raise ValueError(f"Unsupported IGP for FRR render: {igp!r}")


def _render_isis(node: PlannedNode, interfaces: tuple[PlannedInterface, ...]) -> str:
    net = isis_net_from_router_id(node.router_id)
    lines = [
        "!",
        "! FRRouting ISP (IS-IS)",
        "!",
        f"hostname {node.device_name}",
        "!",
        "interface lo",
        " ip router isis NIKA",
        "!",
    ]
    for iface in interfaces:
        lines.extend(
            [
                f"interface {iface.name}",
                " ip router isis NIKA",
                " isis circuit-type level-2-only",
                f" isis metric {iface.metric}",
                "!",
            ]
        )
    lines.extend(
        [
            "router isis NIKA",
            f" net {net}",
            " is-type level-2-only",
            " metric-style wide",
            " passive-interface lo",
        ]
    )
    for iface in interfaces:
        if iface.passive:
            lines.append(f" passive-interface {iface.name}")
    lines.extend(
        [
            "!",
            "log file /var/log/frr/frr.log",
            "!",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_ospf(node: PlannedNode, interfaces: tuple[PlannedInterface, ...]) -> str:
    """Render OSPF using NBMA + explicit neighbors (unicast hellos).

    Kathara/Docker collision domains can drop OSPF multicast at scale, leaving
    adjacencies stuck in Init despite working unicast ICMP. NBMA with
    configured neighbors forces unicast hellos.
    """
    lines = [
        "!",
        "! FRRouting ISP (OSPF)",
        "!",
        f"hostname {node.device_name}",
        "!",
    ]
    for iface in interfaces:
        if iface.passive:
            lines.extend(
                [
                    f"interface {iface.name}",
                    f" ip ospf cost {iface.metric}",
                    "!",
                ]
            )
        else:
            lines.extend(
                [
                    f"interface {iface.name}",
                    " ip ospf network non-broadcast",
                    f" ip ospf cost {iface.metric}",
                    "!",
                ]
            )
    lines.extend(
        [
            "router ospf",
            f" router-id {node.router_id}",
            f" network {node.loopback}/32 area 0.0.0.0",
        ]
    )
    for iface in interfaces:
        lines.append(f" network {iface.subnet} area 0.0.0.0")
    for iface in interfaces:
        if iface.passive:
            lines.append(f" passive-interface {iface.name}")
    for iface in sorted(interfaces, key=lambda item: item.peer_address):
        if iface.passive:
            continue
        # poll-interval keeps retrying until the peer appears after lab boot.
        lines.append(f" neighbor {iface.peer_address} poll-interval 10")
    lines.extend(
        [
            "!",
            "log file /var/log/frr/frr.log",
            "!",
        ]
    )
    return "\n".join(lines) + "\n"
