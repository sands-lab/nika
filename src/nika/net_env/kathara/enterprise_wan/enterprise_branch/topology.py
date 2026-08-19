"""Topology dimensions and intent for enterprise_branch (s/m/l)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from nika.net_env.kathara.enterprise_wan.enterprise_branch.addressing import (
    host_name_for,
    lan_network,
)

TopoSize = Literal["s", "m", "l"]

ASN_HQ = 65000
ASN_DC2 = 65010
LOCAL_PREF_PRIMARY = 200
LOCAL_PREF_BACKUP = 100


@dataclass(frozen=True)
class LanSpec:
    role: str
    advertise: bool
    host_name: str
    prefix: str


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

    def edge_names(self) -> list[str]:
        return [f"{s}_edge" for s in self.sites]

    def isp_names(self) -> list[str]:
        return [f"{p}_core" for p in self.providers]

    def host_names(self) -> list[str]:
        names: list[str] = []
        for site in self.sites.values():
            for lan in site.lans:
                names.append(lan.host_name)
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


def _lans_for(
    site_name: str, site_id: int, roles: tuple[str, ...]
) -> tuple[LanSpec, ...]:
    lans: list[LanSpec] = []
    for role in roles:
        advertise = role != "guest"
        lans.append(
            LanSpec(
                role=role,
                advertise=advertise,
                host_name=host_name_for(site_name, role),
                prefix=str(lan_network(site_id, role)),
            )
        )
    return tuple(lans)


def _branch_asn(branch_idx: int) -> int:
    return ASN_HQ + branch_idx


def build_topo_spec(topo_size: TopoSize) -> TopoSpec:
    if topo_size == "s":
        return _build_small()
    if topo_size == "m":
        return _build_medium()
    if topo_size == "l":
        return _build_large()
    raise ValueError("topo_size should be one of 's', 'm', 'l'.")


def _build_small() -> TopoSpec:
    sites = {
        "hq": SiteSpec(
            name="hq",
            site_id=0,
            asn=ASN_HQ,
            is_hub=True,
            lans=_lans_for("hq", 0, ("corp", "server")),
            wan_providers=("isp1",),
        ),
        "br1": SiteSpec(
            name="br1",
            site_id=1,
            asn=_branch_asn(1),
            is_hub=False,
            lans=_lans_for("br1", 1, ("corp",)),
            wan_providers=("isp1",),
            hub_peers=(("hq", True),),
        ),
        "br2": SiteSpec(
            name="br2",
            site_id=2,
            asn=_branch_asn(2),
            is_hub=False,
            lans=_lans_for("br2", 2, ("corp",)),
            wan_providers=("isp1",),
            hub_peers=(("hq", True),),
        ),
    }
    tunnels = (
        TunnelSpec("wg_hq", "br1", "hq", "isp1", True, LOCAL_PREF_PRIMARY, 51820),
        TunnelSpec("wg_hq", "br2", "hq", "isp1", True, LOCAL_PREF_PRIMARY, 51820),
    )
    # Hub iface names are per-spoke; expand hub side in lab.
    return TopoSpec(size="s", sites=sites, providers=("isp1",), tunnels=tunnels)


def _build_medium() -> TopoSpec:
    sites: dict[str, SiteSpec] = {
        "hq": SiteSpec(
            name="hq",
            site_id=0,
            asn=ASN_HQ,
            is_hub=True,
            lans=_lans_for("hq", 0, ("corp", "server", "guest")),
            wan_providers=("isp1", "isp2"),
        ),
    }
    for i in range(1, 5):
        roles = ("corp", "guest") if i == 1 else ("corp",)
        dual = i <= 2
        sites[f"br{i}"] = SiteSpec(
            name=f"br{i}",
            site_id=i,
            asn=_branch_asn(i),
            is_hub=False,
            lans=_lans_for(f"br{i}", i, roles),
            wan_providers=("isp1", "isp2") if dual else ("isp1",),
            hub_peers=(("hq", True),),
        )
    tunnels: list[TunnelSpec] = []
    for i in range(1, 5):
        tunnels.append(
            TunnelSpec("wg_hq", f"br{i}", "hq", "isp1", True, LOCAL_PREF_PRIMARY, 51820)
        )
        if i <= 2:
            tunnels.append(
                TunnelSpec(
                    "wg_hq_b",
                    f"br{i}",
                    "hq",
                    "isp2",
                    False,
                    LOCAL_PREF_BACKUP,
                    51821,
                )
            )
    return TopoSpec(
        size="m", sites=sites, providers=("isp1", "isp2"), tunnels=tuple(tunnels)
    )


def _build_large() -> TopoSpec:
    sites: dict[str, SiteSpec] = {
        "hq": SiteSpec(
            name="hq",
            site_id=0,
            asn=ASN_HQ,
            is_hub=True,
            lans=_lans_for("hq", 0, ("corp", "server", "guest")),
            wan_providers=("isp1", "isp2"),
        ),
        "dc2": SiteSpec(
            name="dc2",
            site_id=10,
            asn=ASN_DC2,
            is_hub=True,
            lans=_lans_for("dc2", 10, ("corp", "server")),
            wan_providers=("isp1", "isp2"),
        ),
    }
    for i in range(1, 8):
        if i <= 2:
            roles: tuple[str, ...] = ("corp", "guest")
        elif i == 3:
            roles = ("corp", "server")
        else:
            roles = ("corp",)
        dual_wan = i <= 3
        hub_peers: tuple[tuple[str, bool], ...]
        if i <= 3:
            hub_peers = (("hq", True), ("dc2", False))
        else:
            hub_peers = (("hq", True),)
        sites[f"br{i}"] = SiteSpec(
            name=f"br{i}",
            site_id=i,
            asn=_branch_asn(i),
            is_hub=False,
            lans=_lans_for(f"br{i}", i, roles),
            wan_providers=("isp1", "isp2") if dual_wan else ("isp1",),
            hub_peers=hub_peers,
        )
    tunnels: list[TunnelSpec] = []
    for i in range(1, 8):
        tunnels.append(
            TunnelSpec("wg_hq", f"br{i}", "hq", "isp1", True, LOCAL_PREF_PRIMARY, 51820)
        )
        if i <= 3:
            tunnels.append(
                TunnelSpec(
                    "wg_hq_b",
                    f"br{i}",
                    "hq",
                    "isp2",
                    False,
                    LOCAL_PREF_BACKUP,
                    51821,
                )
            )
            tunnels.append(
                TunnelSpec(
                    "wg_dc2",
                    f"br{i}",
                    "dc2",
                    "isp1",
                    False,
                    LOCAL_PREF_BACKUP,
                    51822,
                )
            )
    return TopoSpec(
        size="l", sites=sites, providers=("isp1", "isp2"), tunnels=tuple(tunnels)
    )


def hub_iface_for(spoke: str, hub: str, *, backup: bool = False) -> str:
    """WireGuard interface name on the hub toward a spoke."""
    suffix = "_b" if backup else ""
    return f"wg_{spoke}{suffix}"


def single_path_hq_peer_targets(topo_size: TopoSize) -> list[tuple[str, str]]:
    """Branch edges whose only tunnel is the primary HQ WireGuard peer.

    Dual-homed / multi-hub spokes are excluded so a single peer-key fault is
    not masked by a backup overlay path.
    """
    from nika.net_env.kathara.enterprise_wan.enterprise_branch.addressing import (
        edge_name_for,
    )

    spec = build_topo_spec(topo_size)
    by_spoke: dict[str, list[TunnelSpec]] = {}
    for tunnel in spec.tunnels:
        by_spoke.setdefault(tunnel.local_site, []).append(tunnel)

    targets: list[tuple[str, str]] = []
    for spoke, tunnels in sorted(by_spoke.items()):
        if len(tunnels) != 1:
            continue
        tunnel = tunnels[0]
        if tunnel.remote_site != "hq" or not tunnel.primary:
            continue
        targets.append((edge_name_for(spoke), tunnel.iface))
    return targets
