"""Render enterprise_branch Site Edges as RouterOS 7 auto-import scripts.

The RouterOS edge keeps the FRR edge's forwarding and isolation model:

* The WAN, the provider host routes and the guest NAT pool stay in ``main``,
  like the FRR default VRF's underlay.
* WireGuard tunnels and the overlay eBGP sessions run in the ``overlay`` VRF.
  RouterOS cannot export ``main`` through BGP VPN, so the overlay gets its own
  table.
* BGP VPN route targets leak the overlay's BGP routes into each business VRF
  and export each business VRF's connected LAN to the overlay. This matches
  FRR's ``import vrf default`` and ``import vrf vrf_<role>``: business VRFs
  never import each other's local LANs, and local-only VRFs import nothing.
* Local-only VRFs reach the Internet through a cross-VRF default route,
  source NAT and a forward drop towards 10.0.0.0/8.
* Overlay egress QoS uses packet marks and a queue tree that mirror the HTB
  EF and BE classes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from ipaddress import IPv4Interface

from nika.net_env.enterprise_branch.edge_view import FrrEdgeView
from nika.net_env.enterprise_branch.topology import (
    DSCP_EF,
    LOCAL_ONLY_ROLES,
    OVERLAY_ROLES,
    OverlayQosSpec,
    overlay_be_class,
)
from nika.net_env.isp.routeros import _filter_rule
from nika.net_env.utils.routeros.common import routeros_interface

OVERLAY_VRF = "overlay"
GUEST_NAT_ADDRESS = "198.18.1.10"
_ENTERPRISE = "dst in 10.0.0.0/8 && dst-len <= 24"
_VPN_TARGETS = {"overlay": 1, "corp": 10, "server": 20}


@dataclass(frozen=True)
class RouterOsLan:
    role: str
    vrf: str
    prefix: str
    gateway: str
    iface: str


@dataclass(frozen=True)
class RouterOsTunnel:
    iface: str
    address: str
    peer_address: str
    listen_port: int
    peer_public_key: str
    peer_endpoint: str
    peer_port: int
    remote_asn: int
    local_pref: int


@dataclass(frozen=True)
class RouterOsEdge:
    name: str
    asn: int
    router_id: str
    is_hub: bool
    private_key: str
    lans: tuple[RouterOsLan, ...]
    # (container interface, CIDR, provider gateway) per WAN attachment.
    wans: tuple[tuple[str, str, str], ...]
    public_addresses: Mapping[str, tuple[str, ...]]
    underlay_routes: tuple[tuple[str, str], ...]
    tunnels: tuple[RouterOsTunnel, ...]
    advertised: tuple[str, ...]


def qos_marks(iface: str) -> tuple[str, str]:
    """Return the EF and BE packet marks of one overlay egress."""
    return f"{iface}-ef", f"{iface}-be"


def render_overlay_qos(iface: str, qos: OverlayQosSpec) -> list[str]:
    """Classify EF into a reserved class and cap BE, like the HTB on FRR edges."""
    be_mbit, be_bytes = overlay_be_class(qos)
    ef, be = qos_marks(iface)
    return [
        "/ip firewall mangle add chain=postrouting "
        f"out-interface={iface} dscp={DSCP_EF} action=mark-packet "
        f"new-packet-mark={ef} passthrough=no comment=overlay-qos",
        "/ip firewall mangle add chain=postrouting "
        f"out-interface={iface} action=mark-packet new-packet-mark={be} "
        "passthrough=no comment=overlay-qos",
        f"/queue tree add name={iface} parent={iface} max-limit={qos.rate_mbit}M",
        f"/queue tree add name={ef} parent={iface} packet-mark={ef} "
        f"limit-at={qos.ef_mbit}M max-limit={qos.rate_mbit}M priority=1 "
        "queue=ef-fifo",
        f"/queue tree add name={be} parent={iface} packet-mark={be} "
        f"limit-at={be_mbit}M max-limit={be_mbit}M priority=2 "
        f"queue=be-fifo-{be_bytes}",
    ]


def render_routeros_edge(edge: RouterOsEdge, qos: OverlayQosSpec) -> str:
    lines = [f"/system identity set name={edge.name}"]
    for lan in edge.lans:
        lines.append(
            f"/ip vrf add name={lan.vrf} interfaces={routeros_interface(lan.iface)}"
        )
    # RouterOS creates each VRF's routing table asynchronously; later
    # commands reference the tables by name.
    lines.append(":delay 2s")
    for lan in edge.lans:
        lines.append(
            f"/ip address add address={lan.gateway} "
            f"interface={routeros_interface(lan.iface)}"
        )
    for iface, cidr, _gateway in edge.wans:
        lines.append(
            f"/ip address add address={cidr} interface={routeros_interface(iface)}"
        )
        for address in edge.public_addresses.get(iface, ()):
            lines.append(
                f"/ip address add address={address}/32 "
                f"interface={routeros_interface(iface)}"
            )
    lines.extend(
        f"/ip route add dst-address={dst}/32 gateway={gateway}"
        for dst, gateway in edge.underlay_routes
    )
    lines.extend(_render_tunnels(edge))
    lines.extend(_render_local_only(edge))
    _be_mbit, be_bytes = overlay_be_class(qos)
    lines.extend(
        [
            "/queue type add name=ef-fifo kind=pfifo pfifo-limit=100",
            f"/queue type add name=be-fifo-{be_bytes} kind=bfifo "
            f"bfifo-limit={be_bytes}",
        ]
    )
    for tunnel in edge.tunnels:
        lines.extend(render_overlay_qos(tunnel.iface, qos))
    lines.extend(_render_bgp(edge))
    return "\n".join(lines) + "\n"


def _render_tunnels(edge: RouterOsEdge) -> list[str]:
    lines = []
    for tunnel in edge.tunnels:
        lines.extend(
            [
                f"/interface wireguard add name={tunnel.iface} "
                f'listen-port={tunnel.listen_port} mtu=1420 private-key="{edge.private_key}"',
                f"/interface wireguard peers add interface={tunnel.iface} "
                f'public-key="{tunnel.peer_public_key}" '
                f"endpoint-address={tunnel.peer_endpoint} "
                f"endpoint-port={tunnel.peer_port} allowed-address=0.0.0.0/0 "
                "persistent-keepalive=25s",
            ]
        )
    if edge.tunnels:
        lines.extend(
            [
                f"/ip vrf add name={OVERLAY_VRF} "
                f"interfaces={','.join(t.iface for t in edge.tunnels)}",
                ":delay 2s",
            ]
        )
    lines.extend(
        f"/ip address add address={t.address} interface={t.iface}" for t in edge.tunnels
    )
    return lines


def _render_local_only(edge: RouterOsEdge) -> list[str]:
    local = [lan for lan in edge.lans if lan.role in LOCAL_ONLY_ROLES]
    if not local or not edge.wans:
        return []
    primary_gateway = edge.wans[0][2]
    lines = []
    for lan in local:
        iface = routeros_interface(lan.iface)
        lines.extend(
            [
                f"/ip route add dst-address=0.0.0.0/0 gateway={primary_gateway}@main "
                f"routing-table={lan.vrf}",
                # Replies are de-NATed in main; send them back into the VRF.
                f"/ip route add dst-address={lan.prefix} gateway={iface}@{lan.vrf}",
                f"/ip firewall nat add chain=srcnat src-address={lan.prefix} "
                f"action=src-nat to-addresses={GUEST_NAT_ADDRESS} comment=guest-nat",
                f"/ip firewall filter add chain=forward src-address={lan.prefix} "
                "dst-address=10.0.0.0/8 action=drop comment=guest-nat",
            ]
        )
    return lines


def _render_bgp(edge: RouterOsEdge) -> list[str]:
    if not edge.tunnels:
        return []
    lines = []
    if edge.is_hub:
        lines.append(_filter_rule("POLICY-OUT", f"if ({_ENTERPRISE}) {{ accept; }}"))
    else:
        lines.extend(
            _filter_rule("POLICY-OUT", f"if (dst == {prefix}) {{ accept; }}")
            for prefix in edge.advertised
        )
    lines.append(_filter_rule("POLICY-OUT", "reject;"))
    for tunnel in edge.tunnels:
        chain = f"POLICY-IN-{tunnel.iface}"
        lines.extend(
            [
                _filter_rule(
                    chain,
                    f"if ({_ENTERPRISE}) {{ set bgp-local-pref {tunnel.local_pref}; "
                    "accept; }",
                ),
                _filter_rule(chain, "reject;"),
            ]
        )
    lines.append(
        f"/routing bgp instance add name=CORE as={edge.asn} "
        f"router-id={edge.router_id} vrf={OVERLAY_VRF}"
    )
    for tunnel in edge.tunnels:
        command = (
            f"/routing bgp connection add name={tunnel.iface} instance=CORE "
            f"local.role=ebgp local.address={IPv4Interface(tunnel.address).ip} "
            f"remote.address={tunnel.peer_address} remote.as={tunnel.remote_asn} "
            "afi=ip connect=yes listen=yes "
            f"input.filter=POLICY-IN-{tunnel.iface} output.filter-chain=POLICY-OUT "
            "output.redistribute=bgp-mpls-vpn"
        )
        if edge.is_hub:
            command += " nexthop-choice=force-self"
        lines.append(command)
    lines.extend(_render_vrf_leaking(edge))
    return lines


def _vpn(
    edge: RouterOsEdge,
    vrf: str,
    *,
    imports: Iterable[str],
    export: str,
    redistribute: str,
) -> str:
    targets = ",".join(f"{edge.asn}:{_VPN_TARGETS[item]}" for item in imports)
    return (
        f"/routing bgp vpn add name={vrf} vrf={vrf} instance=CORE "
        f"route-distinguisher={edge.asn}:{_VPN_TARGETS[export]} "
        f"import.route-targets={targets} "
        f"export.route-targets={edge.asn}:{_VPN_TARGETS[export]} "
        f"export.redistribute={redistribute} label-allocation-policy=per-vrf"
    )


def _render_vrf_leaking(edge: RouterOsEdge) -> list[str]:
    business = [lan for lan in edge.lans if lan.role in OVERLAY_ROLES]
    lines = [
        _vpn(
            edge,
            OVERLAY_VRF,
            imports=[lan.role for lan in business],
            export="overlay",
            redistribute="bgp",
        )
    ]
    lines.extend(
        _vpn(
            edge,
            lan.vrf,
            imports=["overlay"],
            export=lan.role,
            redistribute="connected",
        )
        for lan in business
    )
    return lines


class RouterOsEdgeView(FrrEdgeView):
    """Native RouterOS observations for the same enterprise_branch checks."""

    def __init__(self) -> None:
        from nika.net_env.utils.routeros.common import (
            CONFIG_LOG_COMMAND,
            routeros_bgp_peer_query,
        )

        self.bgp_peers = routeros_bgp_peer_query()
        self.routing_process = CONFIG_LOG_COMMAND
        self.bgp_rib = _cli(
            f"/routing/route/print terse where routing-table={OVERLAY_VRF} "
            "and bgp and !filtered"
        )
        self.vrf_list = _cli("/ip/vrf/print terse")

    def routing_process_ok(self, edge: str, output: str) -> bool:
        from nika.net_env.utils.routeros.common import routeros_config_applied

        return routeros_config_applied([(edge, output)])

    def wireguard(self, iface: str) -> str:
        return _cli(f"/interface/wireguard/peers/print terse where interface={iface}")

    def vrf_ribs(self, vrf: str, *, routing_table_first: bool) -> tuple[str, ...]:
        return (
            _cli(
                f"/routing/route/print terse where routing-table={vrf} and afi=ip "
                "and !filtered"
            ),
        )

    def route_lookup(self, vrf: str, address: str, prefix: str) -> str:
        return _cli(
            f"/routing/route/print terse where routing-table={vrf} and active "
            f"and dst-address={prefix}"
        )

    def overlay_path_ok(self, output: str) -> bool:
        return "%wg" in output or "172.30." in output

    def bgp_route(self, prefix: str) -> str:
        return _cli(
            f"/routing/route/print terse where routing-table={OVERLAY_VRF} and bgp "
            f"and active and dst-address={prefix}"
        )

    def qos(self, iface: str) -> tuple[str, ...]:
        return (
            _cli(f"/queue/tree/print terse where parent={iface}"),
            # RouterOS does not filter mangle rows by out-interface; the
            # checks select the interface's rows.
            _cli("/ip/firewall/mangle/print terse where chain=postrouting"),
        )

    def qos_checks(
        self, outputs: tuple[str, ...], qos: OverlayQosSpec
    ) -> dict[str, bool]:
        from nika.net_env.utils.routeros.parse import routeros_terse_rows

        try:
            trees, marks = (routeros_terse_rows(output) for output in outputs)
        except ValueError:
            return dict.fromkeys(("htb", "ef_class", "be_class", "rate"), False)
        by_mark = {row.get("packet-mark", ""): row for row in trees}
        root = next((row for row in trees if row.get("packet-mark", "") == ""), {})
        iface = root.get("name", "")
        ef_mark, be_mark = qos_marks(iface)
        be_mbit, _ = overlay_be_class(qos)
        ef, be = by_mark.get(ef_mark, {}), by_mark.get(be_mark, {})
        classified = {
            row.get("new-packet-mark"): row
            for row in marks
            if row.get("out-interface") == iface and row.get("action") == "mark-packet"
        }
        return {
            "htb": bool(root),
            "ef_class": ef.get("priority") == "1"
            and ef.get("limit-at") == f"{qos.ef_mbit}M"
            and classified.get(ef_mark, {}).get("dscp") == str(DSCP_EF),
            "be_class": be.get("max-limit") == f"{be_mbit}M" and be_mark in classified,
            "rate": root.get("max-limit") == f"{qos.rate_mbit}M",
        }

    def ping(self, target: str) -> str:
        return _cli(f"/ping {target} count=1")

    def ping_ok(self, runtime, edge: str, target: str) -> bool:
        from nika.net_env.verify import exec_or_empty

        output = exec_or_empty(runtime, edge, self.ping(target), timeout=15)
        return "received=1" in output


def _cli(command: str) -> str:
    from nika.service.lab.routeros_api import CLI_COMMAND

    return CLI_COMMAND.format(command=command)
