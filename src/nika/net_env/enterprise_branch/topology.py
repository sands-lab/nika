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

# Backward-compatible aliases (size ``s`` role sets).
HUB_ROLES = HUB_ROLES_S
BRANCH_ROLES = BRANCH_ROLES_S


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
    bulk_flow_count: int


# Overlay egress HTB: total rate, EF class guarantee, competing CS0 flow count.
OVERLAY_QOS: dict[TopoSize, OverlayQosSpec] = {
    "s": OverlayQosSpec(rate_mbit=8, ef_mbit=2, bulk_flow_count=2),
    "m": OverlayQosSpec(rate_mbit=16, ef_mbit=3, bulk_flow_count=4),
    "l": OverlayQosSpec(rate_mbit=32, ef_mbit=4, bulk_flow_count=6),
}

# DSCP EF = 46 → TOS 0xb8; mask 0xfc ignores ECN.
DSCP_EF = 46
DSCP_CS0 = 0
TOS_EF = 0xB8
TOS_CS0 = 0x00


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


def overlay_qos_for(topo_size: TopoSize) -> OverlayQosSpec:
    if topo_size not in OVERLAY_QOS:
        raise ValueError("topo_size should be one of 's', 'm', 'l'.")
    return OVERLAY_QOS[topo_size]


def overlay_qos_startup_cmds(iface: str, qos: OverlayQosSpec) -> list[str]:
    """HTB dual-class QoS on a WireGuard overlay egress (EF vs default/BE).

    EF keeps a reserved class with a deep queue. BE is hard-capped at its rate
    with a deep byte-FIFO so saturating CS0 bulk builds ~500ms standing delay;
    demoted EF then sees clear latency (and loss under overload), not just
    fq_codel jitter on an otherwise empty sparse-flow queue.
    """
    be_mbit = max(qos.rate_mbit - qos.ef_mbit, 1)
    # ~500ms of standing queue at the BE ceil (bytes = rate_bps * 0.5 / 8).
    be_bfifo_bytes = max(int(be_mbit * 1_000_000 * 0.5 / 8), 64_000)
    return [
        f"tc qdisc add dev {iface} root handle 1: htb default 20 || true",
        f"tc class add dev {iface} parent 1: classid 1:1 "
        f"htb rate {qos.rate_mbit}mbit ceil {qos.rate_mbit}mbit || true",
        # EF: reserved rate, may borrow unused BE capacity up to link ceil.
        f"tc class add dev {iface} parent 1:1 classid 1:10 "
        f"htb rate {qos.ef_mbit}mbit ceil {qos.rate_mbit}mbit prio 1 || true",
        # BE: hard-capped so bulk cannot silently use EF's reservation.
        f"tc class add dev {iface} parent 1:1 classid 1:20 "
        f"htb rate {be_mbit}mbit ceil {be_mbit}mbit prio 2 || true",
        f"tc filter add dev {iface} parent 1: protocol ip prio 1 u32 "
        f"match ip tos {TOS_EF:#x} 0xfc flowid 1:10 || true",
        f"tc qdisc add dev {iface} parent 1:10 handle 10: pfifo limit 100 || true",
        # Deep byte-FIFO: bulk keeps ~500ms standing queue for demoted EF.
        f"tc qdisc add dev {iface} parent 1:20 handle 20: "
        f"bfifo limit {be_bfifo_bytes} || true",
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


def corp_pairs_sharing_overlay_egress(
    topo_size: TopoSize,
    *,
    edge: str,
    intf_name: str,
    exclude: tuple[str, str] | None = None,
) -> list[tuple[str, str]]:
    """CORP host pairs whose primary path leaves ``edge`` via ``intf_name``.

    Used to build competing CS0 bulk flows on the same overlay egress as the EF
    foreground path. ``exclude`` skips the EF (src, dst) pair when provided.
    """
    spec = build_topo_spec(topo_size)
    site = edge[: -len("_edge")] if edge.endswith("_edge") else edge
    if site not in spec.sites:
        return []

    local_hosts = list(_corp_hosts(spec, site))
    pairs: list[tuple[str, str]] = []

    if site in spec.branch_names() and intf_name == "wg_hq":
        # Primary path: branch CORP → HQ / DC2 / other branches (hairpin via HQ).
        remotes: list[str] = []
        remotes.extend(_corp_hosts(spec, "hq"))
        remotes.extend(_corp_hosts(spec, "dc2"))
        for other in spec.branch_names():
            if other == site:
                continue
            remotes.extend(_corp_hosts(spec, other))
        for src in local_hosts:
            for dst in remotes:
                pairs.append((src, dst))
    elif site == "hq" and intf_name.startswith("wg_br"):
        spoke = intf_name[len("wg_") :].removesuffix("_b")
        if spoke in spec.branch_names():
            for src in local_hosts:
                for dst in _corp_hosts(spec, spoke):
                    pairs.append((src, dst))
    elif site == "dc2" and intf_name.startswith("wg_br"):
        spoke = intf_name[len("wg_") :].removesuffix("_b")
        if spoke in spec.branch_names():
            for src in local_hosts:
                for dst in _corp_hosts(spec, spoke):
                    pairs.append((src, dst))
    elif site == "hq" and intf_name in {"wg_dc2", "wg_dc2_b"}:
        for src in local_hosts:
            for dst in _corp_hosts(spec, "dc2"):
                pairs.append((src, dst))

    if exclude is not None:
        pairs = [p for p in pairs if p != exclude]

    # Prefer diversity; keep stable order for deterministic benchmarks.
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        ordered.append(pair)
    return ordered
