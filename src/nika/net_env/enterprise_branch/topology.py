"""Topology dimensions and intent for enterprise_branch (s/m/l).

One production template for all sizes. s/m/l scale branch count and hosts per
LAN; m/l also add an IOT VRF so complexity grows with business domains, not
only replicated VLANs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nika.net_env.enterprise_branch.addressing import (
    host_name_for,
    lan_network,
    vrf_name,
)

TopoSize = Literal["s", "m", "l"]

ASN_HQ = 65000
ASN_DC2 = 65010
LOCAL_PREF_PRIMARY = 200
LOCAL_PREF_BACKUP = 100

PROVIDERS = ("isp1", "isp2")

# Local-only VRFs: no overlay BGP advertise.
LOCAL_ONLY_ROLES = frozenset({"guest", "iot"})
# Overlay-participating roles (may also participate in shared-services leak).
OVERLAY_ROLES = frozenset({"corp", "server"})

HUB_ROLES_S = ("corp", "server", "guest")
BRANCH_ROLES_S = ("corp", "guest")
HUB_ROLES_ML = ("corp", "server", "iot", "guest")
BRANCH_ROLES_ML = ("corp", "iot", "guest")


@dataclass(frozen=True)
class ScaleSpec:
    """Scale knobs for one topo size."""

    branches: int
    hosts_per_lan: int
    hub_roles: tuple[str, ...]
    branch_roles: tuple[str, ...]


@dataclass(frozen=True)
class OverlayQosSpec:
    """Per-size WireGuard overlay egress capacity and EF reservation (mbit)."""

    rate_mbit: int
    ef_mbit: int


# Overlay egress HTB: total rate and EF class guarantee. The EF guarantee
# covers the largest call each egress carries (the br1 video link), so healthy
# media never borrows; best effort gets the rest of the link.
OVERLAY_QOS: dict[TopoSize, OverlayQosSpec] = {
    "s": OverlayQosSpec(rate_mbit=8, ef_mbit=4),
    "m": OverlayQosSpec(rate_mbit=16, ef_mbit=7),
    # l keeps the m rate: eight branches share the same hubs and underlay, and
    # a faster regional egress loads them enough to delay healthy traffic.
    "l": OverlayQosSpec(rate_mbit=16, ef_mbit=7),
}

# DSCP EF = 46 → TOS 0xb8; mask 0xfc ignores ECN.
DSCP_EF = 46
DSCP_CS0 = 0
TOS_EF = 0xB8


SCALE: dict[TopoSize, ScaleSpec] = {
    "s": ScaleSpec(
        branches=2,
        hosts_per_lan=1,
        hub_roles=HUB_ROLES_S,
        branch_roles=BRANCH_ROLES_S,
    ),
    "m": ScaleSpec(
        branches=4,
        hosts_per_lan=2,
        hub_roles=HUB_ROLES_ML,
        branch_roles=BRANCH_ROLES_ML,
    ),
    "l": ScaleSpec(
        branches=8,
        hosts_per_lan=4,
        hub_roles=HUB_ROLES_ML,
        branch_roles=BRANCH_ROLES_ML,
    ),
}


# Regional underlay one-way delay on each Site Edge↔provider attachment.
# Branch→HQ crosses two attachments (~40 ms one-way, ~80 ms RTT), under the
# Cisco SD-WAN Small Branch Bulk-Data (300 ms) and Business-Data (400 ms) RTT
# SLA ceilings. See docs/operations/network-scenarios.md.
UNDERLAY_ONE_WAY_DELAY_MS = 20
# Branch→branch crosses the hub: four attachments each way, ~160 ms RTT.
MAX_OVERLAY_RTT_MS = 8 * UNDERLAY_ONE_WAY_DELAY_MS

# Static objects served by SERVER-role HTTP hosts. large.bin downloads in a
# few seconds over a healthy branch path, so a capped test fetch completes.
HTTP_SMALL_OBJECT_KB = 16
HTTP_LARGE_OBJECT_MB = 1
# File-share archive the branch sync clients pull: long transfers that keep
# the bulk load steady.
SYNC_OBJECT = "projects.tar"
SYNC_OBJECT_MB = 32

# Public web/push service that GUEST and IOT clients reach through their Site
# Edge NAT. The primary provider (``isp1``) hosts it on a loopback address.
PUBLIC_SERVICE_IP = "198.51.100.10"


def overlay_qos_for(topo_size: TopoSize) -> OverlayQosSpec:
    if topo_size not in OVERLAY_QOS:
        raise ValueError("topo_size should be one of 's', 'm', 'l'.")
    return OVERLAY_QOS[topo_size]


def overlay_be_class(qos: OverlayQosSpec) -> tuple[int, int]:
    """Return the BE class ceiling (mbit) and its byte-FIFO depth."""
    be_mbit = max(qos.rate_mbit - qos.ef_mbit, 1)
    # ~500ms of standing queue at the BE ceil (bytes = rate_bps * 0.5 / 8).
    return be_mbit, max(int(be_mbit * 1_000_000 * 0.5 / 8), 64_000)


def overlay_qos_startup_cmds(iface: str, qos: OverlayQosSpec) -> list[str]:
    """HTB dual-class QoS on a WireGuard overlay egress (EF vs default/BE).

    EF gets a reserved high-priority class. BE is hard-capped at the rest of
    the link and served by fq_codel, as in WAN-edge QoS with fair queueing and
    AQM on the default class (Cisco ``fair-queue`` in class-default, OpenWrt
    SQM ``simple.qos``): bulk transfers keep no standing queue and sparse
    flows such as pings skip ahead, while a flow that sends above its fair
    share of the class is dropped.
    """
    be_mbit, _ = overlay_be_class(qos)
    # Token buckets hold 10 ms at the link rate, like a shaper's Bc = CIR x Tc,
    # so back-to-back packets of a flow within its rate are not throttled.
    burst = f"burst {qos.rate_mbit * 1250} cburst {qos.rate_mbit * 1250}"
    return [
        f"tc qdisc add dev {iface} root handle 1: htb default 20 || true",
        f"tc class add dev {iface} parent 1: classid 1:1 "
        f"htb rate {qos.rate_mbit}mbit ceil {qos.rate_mbit}mbit {burst} || true",
        # EF: reserved rate, may borrow unused BE capacity up to link ceil.
        f"tc class add dev {iface} parent 1:1 classid 1:10 "
        f"htb rate {qos.ef_mbit}mbit ceil {qos.rate_mbit}mbit {burst} prio 1 || true",
        # BE: hard-capped so bulk cannot silently use EF's reservation.
        f"tc class add dev {iface} parent 1:1 classid 1:20 "
        f"htb rate {be_mbit}mbit ceil {be_mbit}mbit {burst} prio 2 || true",
        f"tc filter add dev {iface} parent 1: protocol ip prio 1 u32 "
        f"match ip tos {TOS_EF:#x} 0xfc flowid 1:10 || true",
        f"tc qdisc add dev {iface} parent 1:10 handle 10: pfifo limit 100 || true",
        f"tc qdisc add dev {iface} parent 1:20 handle 20: fq_codel || true",
    ]


@dataclass(frozen=True)
class LanSpec:
    role: str
    advertise: bool
    host_names: tuple[str, ...]
    prefix: str

    @property
    def vrf(self) -> str:
        """Linux VRF device name for this LAN role."""
        return vrf_name(self.role)

    @property
    def host_name(self) -> str:
        """Primary (index-0) host; kept for callers that need one endpoint."""
        return self.host_names[0]


@dataclass(frozen=True)
class SiteSpec:
    name: str
    site_id: int
    asn: int
    is_hub: bool
    lans: tuple[LanSpec, ...]
    wan_providers: tuple[str, ...]  # primary first
    # For spokes: (hub_site_name, primary) peer intent
    hub_peers: tuple[tuple[str, bool], ...] = ()

    def vrf_roles(self) -> tuple[str, ...]:
        return tuple(lan.role for lan in self.lans)


@dataclass(frozen=True)
class TunnelSpec:
    """One site-to-site WireGuard + eBGP adjacency over a provider underlay."""

    iface: str
    local_site: str
    remote_site: str
    provider: str
    primary: bool
    local_pref: int
    listen_port: int


@dataclass(frozen=True)
class BuiltTunnel:
    """Resolved tunnel endpoints after lab addressing."""

    spoke: str
    hub: str
    provider: str
    primary: bool
    local_pref: int
    spoke_iface: str
    hub_iface: str
    spoke_tunnel_ip: str
    hub_tunnel_ip: str
    spoke_wan_ip: str
    hub_wan_ip: str
    listen_port_spoke: int
    listen_port_hub: int


@dataclass(frozen=True)
class TopoSpec:
    size: TopoSize
    sites: dict[str, SiteSpec]
    providers: tuple[str, ...]
    tunnels: tuple[TunnelSpec, ...]
    hosts_per_lan: int

    def edge_names(self) -> list[str]:
        return [f"{s}_edge" for s in self.sites]

    def isp_names(self) -> list[str]:
        return [f"{p}_core" for p in self.providers]

    def host_names(self) -> list[str]:
        names: list[str] = []
        for site in self.sites.values():
            for lan in site.lans:
                names.extend(lan.host_names)
        return names

    def all_node_names(self) -> list[str]:
        return self.edge_names() + self.isp_names() + self.host_names()

    def advertised_prefixes(self) -> list[str]:
        return [
            lan.prefix
            for site in self.sites.values()
            for lan in site.lans
            if lan.advertise
        ]

    def guest_prefixes(self) -> list[str]:
        return [
            lan.prefix
            for site in self.sites.values()
            for lan in site.lans
            if lan.role == "guest"
        ]

    def local_only_prefixes(self) -> list[str]:
        """GUEST/IOT prefixes that must not appear in overlay BGP."""
        return [
            lan.prefix
            for site in self.sites.values()
            for lan in site.lans
            if lan.role in LOCAL_ONLY_ROLES
        ]

    def prefixes_for_role(self, role: str) -> list[str]:
        return [
            lan.prefix
            for site in self.sites.values()
            for lan in site.lans
            if lan.role == role
        ]

    def branch_names(self) -> list[str]:
        return [name for name, site in self.sites.items() if not site.is_hub]


def _lans_for(
    site_name: str,
    site_id: int,
    roles: tuple[str, ...],
    *,
    hosts_per_lan: int,
) -> tuple[LanSpec, ...]:
    lans: list[LanSpec] = []
    for role in roles:
        advertise = role in OVERLAY_ROLES
        # SERVER stays a single HTTP anchor on hubs.
        n_hosts = 1 if role == "server" else hosts_per_lan
        host_names = tuple(
            host_name_for(site_name, role, host_index=i) for i in range(n_hosts)
        )
        lans.append(
            LanSpec(
                role=role,
                advertise=advertise,
                host_names=host_names,
                prefix=str(lan_network(site_id, role)),
            )
        )
    return tuple(lans)


def _branch_asn(branch_idx: int) -> int:
    return ASN_HQ + branch_idx


def build_topo_spec(topo_size: TopoSize) -> TopoSpec:
    if topo_size not in SCALE:
        raise ValueError("topo_size should be one of 's', 'm', 'l'.")
    scale = SCALE[topo_size]
    return _build_scaled(topo_size, scale)


def _build_scaled(topo_size: TopoSize, scale: ScaleSpec) -> TopoSpec:
    sites: dict[str, SiteSpec] = {
        "hq": SiteSpec(
            name="hq",
            site_id=0,
            asn=ASN_HQ,
            is_hub=True,
            lans=_lans_for("hq", 0, scale.hub_roles, hosts_per_lan=scale.hosts_per_lan),
            wan_providers=PROVIDERS,
        ),
        "dc2": SiteSpec(
            name="dc2",
            site_id=10,
            asn=ASN_DC2,
            is_hub=True,
            lans=_lans_for(
                "dc2", 10, scale.hub_roles, hosts_per_lan=scale.hosts_per_lan
            ),
            wan_providers=PROVIDERS,
        ),
    }
    hub_peers: tuple[tuple[str, bool], ...] = (("hq", True), ("dc2", False))
    for i in range(1, scale.branches + 1):
        sites[f"br{i}"] = SiteSpec(
            name=f"br{i}",
            site_id=i,
            asn=_branch_asn(i),
            is_hub=False,
            lans=_lans_for(
                f"br{i}", i, scale.branch_roles, hosts_per_lan=scale.hosts_per_lan
            ),
            wan_providers=PROVIDERS,
            hub_peers=hub_peers,
        )

    tunnels: list[TunnelSpec] = [
        # Hub interconnect (HQ ↔ DC2), dual-provider.
        TunnelSpec("wg_dc2", "hq", "dc2", "isp1", True, LOCAL_PREF_PRIMARY, 51820),
        TunnelSpec("wg_dc2_b", "hq", "dc2", "isp2", False, LOCAL_PREF_BACKUP, 51821),
    ]
    for i in range(1, scale.branches + 1):
        spoke = f"br{i}"
        tunnels.append(
            TunnelSpec("wg_hq", spoke, "hq", "isp1", True, LOCAL_PREF_PRIMARY, 51820)
        )
        tunnels.append(
            TunnelSpec("wg_hq_b", spoke, "hq", "isp2", False, LOCAL_PREF_BACKUP, 51821)
        )
        tunnels.append(
            TunnelSpec("wg_dc2", spoke, "dc2", "isp1", False, LOCAL_PREF_BACKUP, 51822)
        )

    return TopoSpec(
        size=topo_size,
        sites=sites,
        providers=PROVIDERS,
        tunnels=tuple(tunnels),
        hosts_per_lan=scale.hosts_per_lan,
    )


def hub_iface_for(spoke: str, hub: str, *, backup: bool = False) -> str:
    """WireGuard interface name on the hub toward a peer (spoke or other hub)."""
    del hub  # naming is peer-based on every hub
    suffix = "_b" if backup else ""
    return f"wg_{spoke}{suffix}"


def primary_hq_peer_targets(topo_size: TopoSize) -> list[tuple[str, str]]:
    """Branch edges with a primary HQ WireGuard peer (all branches)."""
    from nika.net_env.enterprise_branch.addressing import (
        edge_name_for,
    )

    spec = build_topo_spec(topo_size)
    targets: list[tuple[str, str]] = []
    for tunnel in spec.tunnels:
        if tunnel.remote_site != "hq" or not tunnel.primary:
            continue
        if tunnel.local_site not in spec.branch_names():
            continue
        targets.append((edge_name_for(tunnel.local_site), tunnel.iface))
    return targets


def single_path_hq_peer_targets(topo_size: TopoSize) -> list[tuple[str, str]]:
    """Deprecated: full-redundancy template has no single-path spokes.

    Kept as an alias of ``primary_hq_peer_targets`` for callers that still import
    this name; peer-key inject now breaks every WG iface on the spoke.
    """
    return primary_hq_peer_targets(topo_size)


def remote_advertised_prefixes_for_spoke(topo_size: TopoSize, spoke: str) -> list[str]:
    """Advertised CORP/SERVER prefixes not owned by ``spoke``."""
    spec = build_topo_spec(topo_size)
    local = {lan.prefix for lan in spec.sites[spoke].lans if lan.advertise}
    return [p for p in spec.advertised_prefixes() if p not in local]


@dataclass(frozen=True)
class DscpRemarkTarget:
    """One eligible LAN→overlay DSCP remark inject target with EF path endpoints."""

    edge: str
    intf_name: str
    site: str
    src_host: str
    dst_host: str
    corp_prefix: str


def _corp_lan(spec: TopoSpec, site: str) -> LanSpec:
    return next(lan for lan in spec.sites[site].lans if lan.role == "corp")


def _corp_hosts(spec: TopoSpec, site: str) -> tuple[str, ...]:
    return _corp_lan(spec, site).host_names


def dscp_remark_inject_targets(topo_size: TopoSize) -> list[DscpRemarkTarget]:
    """Eligible (Site Edge, primary overlay egress, EF CORP path) inject targets.

    Covers Branch ``wg_hq`` (Branch→HQ), HQ ``wg_brN`` (HQ→Branch), and DC2
    ``wg_brN`` (DC2→Branch). Each path's LAN→overlay egress is the named iface.
    """
    from nika.net_env.enterprise_branch.addressing import (
        edge_name_for,
    )

    spec = build_topo_spec(topo_size)
    hq_corp = _corp_hosts(spec, "hq")
    dc2_corp = _corp_hosts(spec, "dc2")
    targets: list[DscpRemarkTarget] = []

    for spoke in spec.branch_names():
        spoke_corp = _corp_hosts(spec, spoke)
        spoke_lan = _corp_lan(spec, spoke)
        targets.append(
            DscpRemarkTarget(
                edge=edge_name_for(spoke),
                intf_name="wg_hq",
                site=spoke,
                src_host=spoke_corp[0],
                dst_host=hq_corp[0],
                corp_prefix=spoke_lan.prefix,
            )
        )
        # Hub-originated CORP toward this spoke (primary tunnel iface on hub).
        hq_lan = _corp_lan(spec, "hq")
        targets.append(
            DscpRemarkTarget(
                edge=edge_name_for("hq"),
                intf_name=hub_iface_for(spoke, "hq"),
                site="hq",
                src_host=hq_corp[0],
                dst_host=spoke_corp[0],
                corp_prefix=hq_lan.prefix,
            )
        )
        dc2_lan = _corp_lan(spec, "dc2")
        targets.append(
            DscpRemarkTarget(
                edge=edge_name_for("dc2"),
                intf_name=hub_iface_for(spoke, "dc2"),
                site="dc2",
                src_host=dc2_corp[0],
                dst_host=spoke_corp[0],
                corp_prefix=dc2_lan.prefix,
            )
        )
    return targets


@dataclass(frozen=True)
class HijackTarget:
    """A BGP origination that wins over the owner's route at remote sites."""

    hijacker: str
    prefix: str
    observer: str
    observer_host: str
    probe_ip: str


def bgp_hijack_targets(topo_size: TopoSize) -> list[HijackTarget]:
    """HQ edge originating another branch's CORP /24.

    Hubs accept only up to /24 from the overlay, so a more-specific never
    leaves the hijacker, and a branch-originated /24 loses to the owner's older
    path at the hubs. A /24 HQ originates itself beats the owner's path at HQ
    (locally originated) and at every other branch (primary local-pref), so
    their traffic to that branch's CORP LAN ends at HQ.
    """
    from nika.net_env.enterprise_branch.addressing import edge_name_for

    spec = build_topo_spec(topo_size)
    branches = spec.branch_names()
    targets: list[HijackTarget] = []
    for victim in branches:
        observer = next(b for b in branches if b != victim)
        victim_lan = _corp_lan(spec, victim)
        probe_ip = victim_lan.prefix.rsplit(".", 1)[0] + ".2"
        targets.append(
            HijackTarget(
                hijacker=edge_name_for("hq"),
                prefix=victim_lan.prefix,
                observer=edge_name_for(observer),
                observer_host=_corp_hosts(spec, observer)[0],
                probe_ip=probe_ip,
            )
        )
    return targets
