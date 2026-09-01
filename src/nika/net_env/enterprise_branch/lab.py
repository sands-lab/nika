"""Enterprise Branch VPN / WAN: hub-and-spoke overlay over provider underlay."""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Interface, IPv4Network

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase
from nika.net_env.enterprise_branch.addressing import (
    VRF_TABLE,
    edge_name_for,
    isp_name_for,
    lan_edge_ip,
    lan_host_ip,
    vrf_name,
)
from nika.net_env.enterprise_branch.topology import (
    HTTP_LARGE_OBJECT_MB,
    HTTP_SMALL_OBJECT_KB,
    LOCAL_ONLY_ROLES,
    OVERLAY_ROLES,
    UNDERLAY_ONE_WAY_DELAY_MS,
    BuiltTunnel,
    TunnelSpec,
    TopoSpec,
    TopoSize,
    build_topo_spec,
    hub_iface_for,
    overlay_qos_for,
    overlay_qos_startup_cmds,
)
from nika.net_env.enterprise_branch.wireguard import (
    load_key_pairs,
    render_wg_conf,
)
from nika.runtime.spec import NodeRole

PROVIDER_DAEMONS = """\
zebra=yes
bgpd=no
ospfd=no
ospf6d=no
ripd=no
ripngd=no
isisd=no
pimd=no
ldpd=no
nhrpd=no
eigrpd=no
babeld=no
sharpd=no
staticd=no
pbrd=no
bfdd=no
fabricd=no
vtysh_enable=yes
zebra_options=" -s 90000000 --daemon -A 127.0.0.1"
"""


@dataclass
class EdgeRuntime:
    name: str
    site: str
    machine: Machine
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    # provider -> (local_wan_cidr, isp_pe_ip, local_wan_ip, interface)
    wan: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    # iface -> (local_tunnel_cidr, peer_tunnel_ip, listen_port, remote_site, primary, local_pref, remote_wan_ip)
    tunnels: dict[str, tuple[str, str, int, str, bool, int, str]] = field(
        default_factory=dict
    )
    key_idx: int = 0


@dataclass
class IspRuntime:
    name: str
    provider: str
    machine: Machine
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)


@dataclass
class HostRuntime:
    name: str
    machine: Machine
    cmd_list: list[str] = field(default_factory=list)
    ip: str = ""
    role: str = ""
    site: str = ""


def _render_edge_frr(
    *,
    hostname: str,
    asn: int,
    router_id: str,
    is_hub: bool,
    overlay_roles: tuple[str, ...],
    local_adv_prefixes: list[str],
    tunnels: dict[str, tuple[str, str, int, str, bool, int, str]],
    peer_asns: dict[str, int],
) -> str:
    """Build FRR conf: default-VRF eBGP overlay + per-business-VRF import/export."""
    lines: list[str] = [
        "!",
        f"hostname {hostname}",
        "!",
        "log file /var/log/frr/frr.log",
        "!",
        "ip prefix-list ENTERPRISE seq 5 permit 10.0.0.0/8 le 24",
        "!",
    ]
    seq = 5
    if local_adv_prefixes:
        for prefix in local_adv_prefixes:
            lines.append(f"ip prefix-list LOCAL-ADV seq {seq} permit {prefix}")
            seq += 5
    else:
        lines.append("ip prefix-list LOCAL-ADV seq 5 deny 0.0.0.0/0 le 32")
    lines.append("!")
    lines.extend(
        [
            "route-map RM-OUT-LOCAL permit 10",
            " match ip address prefix-list LOCAL-ADV",
            "!",
            "route-map RM-OUT-TRANSIT permit 10",
            " match ip address prefix-list ENTERPRISE",
            "!",
        ]
    )

    out_map = "RM-OUT-TRANSIT" if is_hub else "RM-OUT-LOCAL"
    for iface, (
        _local_cidr,
        _peer_tun_ip,
        _port,
        _remote_site,
        _primary,
        local_pref,
        _rwan,
    ) in tunnels.items():
        lines.extend(
            [
                f"route-map RM-IN-{iface} permit 10",
                " match ip address prefix-list ENTERPRISE",
                f" set local-preference {local_pref}",
                "!",
            ]
        )

    for role in overlay_roles:
        vname = vrf_name(role)
        lines.extend([f"vrf {vname}", "exit-vrf", "!"])

    lines.extend(
        [
            f"router bgp {asn}",
            f" bgp router-id {router_id}",
            " no bgp ebgp-requires-policy",
        ]
    )
    for iface, (
        local_cidr,
        peer_tun_ip,
        _port,
        remote_site,
        _primary,
        _local_pref,
        _rwan,
    ) in tunnels.items():
        remote_asn = peer_asns[remote_site]
        local_ip = local_cidr.split("/")[0]
        map_name = f"RM-IN-{iface}"
        lines.append(f" neighbor {peer_tun_ip} remote-as {remote_asn}")
        lines.append(f" neighbor {peer_tun_ip} update-source {local_ip}")
        lines.append(f" neighbor {peer_tun_ip} route-map {out_map} out")
        lines.append(f" neighbor {peer_tun_ip} route-map {map_name} in")
        if is_hub:
            lines.append(f" neighbor {peer_tun_ip} next-hop-self")
    lines.extend([" !", " address-family ipv4 unicast"])
    for role in overlay_roles:
        lines.append(f"  import vrf {vrf_name(role)}")
    for _iface, (
        _local_cidr,
        peer_tun_ip,
        _port,
        _remote_site,
        _primary,
        _local_pref,
        _rwan,
    ) in tunnels.items():
        lines.append(f"  neighbor {peer_tun_ip} activate")
    lines.extend([" exit-address-family", "!", "!"])

    for role in overlay_roles:
        vname = vrf_name(role)
        lines.extend(
            [
                f"router bgp {asn} vrf {vname}",
                f" bgp router-id {router_id}",
                " no bgp ebgp-requires-policy",
                " !",
                " address-family ipv4 unicast",
                "  redistribute connected",
                # FRR 10.x accepts `import vrf default` only (no inline route-map).
                # Guest/IOT have no BGP VRF instance, so they never import overlay.
                "  import vrf default",
                " exit-address-family",
                "!",
                "!",
            ]
        )

    lines.extend(["line vty", "!", ""])
    return "\n".join(lines)


class EnterpriseBranch(NetworkEnvBase):
    LAB_NAME = "enterprise_branch"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = [
        "arp",
        "link",
        "mac",
        "icmp",
        "frr",
        "bgp",
        "pc",
        "http",
        "vpn",
        "nat",
        "forwarding_device",
    ]
    VERIFY_MAX_WAIT_SEC = 240

    def __init__(self, topo_size: TopoSize = "s", **kwargs):
        super().__init__(**kwargs)
        self.topo_size: TopoSize = topo_size
        self.spec: TopoSpec = build_topo_spec(topo_size)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.built_tunnels: list[BuiltTunnel] = []
        self._edges: dict[str, EdgeRuntime] = {}
        self._isps: dict[str, IspRuntime] = {}
        self._hosts: dict[str, HostRuntime] = {}
        self._key_pairs = load_key_pairs()
        self._wan_pool = list(IPv4Network("100.64.0.0/16").subnets(new_prefix=30))
        self._tun_pool = list(IPv4Network("172.30.0.0/16").subnets(new_prefix=30))
        # Per-edge WG ListenPort allocator. Hubs that also terminate spoke-side
        # interconnect tunnels (HQ) must not reuse TunnelSpec.listen_port.
        self._edge_listen_ports: dict[str, int] = {}

        self._create_nodes()
        self._wire_lans()
        self._wire_wan()
        self._build_tunnels()
        self._write_edge_configs()
        self._write_isp_configs()
        self._write_host_configs()

        self.desc = (
            "Enterprise hub-and-spoke Branch VPN over provider IP underlay. "
            "Site Edge routers terminate WireGuard tunnels and run eBGP for "
            "authorized business prefixes in per-role VRFs. Providers only "
            "forward tunnel endpoint reachability."
        )

    def _next_wan(self) -> IPv4Network:
        return self._wan_pool.pop(0)

    def _next_tun(self) -> IPv4Network:
        return self._tun_pool.pop(0)

    def _next_listen_port(self, site: str) -> int:
        """Allocate a unique WireGuard ListenPort on ``site``'s Site Edge."""
        port = self._edge_listen_ports.get(site, 51820)
        self._edge_listen_ports[site] = port + 1
        return port

    def _create_nodes(self) -> None:
        key_i = 0
        for site_name in self.spec.sites:
            ename = edge_name_for(site_name)
            machine = self.lab.new_machine(
                ename, **{"image": "nika/frr", "cpus": 0.5, "mem": "256m"}
            )
            self.declare_machine(
                ename,
                role=NodeRole.ROUTER,
                capabilities=("linux", "frr", "bgp", "wireguard"),
            )
            self._edges[site_name] = EdgeRuntime(
                name=ename, site=site_name, machine=machine, key_idx=key_i
            )
            key_i += 1

        for provider in self.spec.providers:
            iname = isp_name_for(provider)
            machine = self.lab.new_machine(
                iname, **{"image": "nika/frr", "cpus": 0.5, "mem": "256m"}
            )
            self.declare_machine(
                iname,
                role=NodeRole.ROUTER,
                capabilities=("linux", "frr"),
            )
            self._isps[provider] = IspRuntime(
                name=iname, provider=provider, machine=machine
            )

        for site in self.spec.sites.values():
            for lan in site.lans:
                for host_name in lan.host_names:
                    machine = self.lab.new_machine(
                        host_name,
                        **{
                            "image": "nika/base",
                            "cpus": 0.5,
                            "mem": "256m",
                        },
                    )
                    # Privileged: allow runtime net.ipv4.tcp_* sysctl writes used by
                    # tcp_receive_window_limited (Docker mounts those read-only
                    # otherwise).
                    machine.add_meta("privileged", True)
                    self.declare_machine(
                        host_name,
                        role=NodeRole.HOST,
                        capabilities=("linux",),
                        reachability_target=True,
                    )
                    self._hosts[host_name] = HostRuntime(
                        name=host_name,
                        machine=machine,
                        role=lan.role,
                        site=site.name,
                    )

    def _connect(self, a: str, b: str, link: str) -> None:
        self.lab.connect_machine_to_link(a, link)
        self.lab.connect_machine_to_link(b, link)

    def _wire_lans(self) -> None:
        for site in self.spec.sites.values():
            edge = self._edges[site.name]
            # Create Linux VRFs before enslaving LAN interfaces.
            for lan in site.lans:
                table = VRF_TABLE[lan.role]
                edge.cmd_list.append(f"ip link add {lan.vrf} type vrf table {table}")
                edge.cmd_list.append(f"ip link set dev {lan.vrf} up")
            for lan in site.lans:
                link = f"{edge.name}_{site.name}_{lan.role}"
                self.lab.connect_machine_to_link(edge.name, link)
                eth = f"eth{edge.eth_index}"
                e_ip = lan_edge_ip(site.site_id, lan.role)
                edge.cmd_list.append(f"ip link set dev {eth} master {lan.vrf}")
                edge.cmd_list.append(f"ip addr add {e_ip} dev {eth}")
                edge.cmd_list.append(f"ip link set dev {eth} up")
                edge.eth_index += 1
                for host_index, host_name in enumerate(lan.host_names):
                    host = self._hosts[host_name]
                    self.lab.connect_machine_to_link(host_name, link)
                    h_ip = lan_host_ip(site.site_id, lan.role, host_index)
                    host.cmd_list.append(f"ip addr add {h_ip} dev eth0")
                    host.cmd_list.append(f"ip route add default via {e_ip.ip} dev eth0")
                    host.ip = str(h_ip.ip)
                    if lan.role == "server":
                        host.cmd_list.extend(
                            [
                                "mkdir -p /var/www",
                                f"dd if=/dev/zero of=/var/www/small.bin "
                                f"bs=1024 count={HTTP_SMALL_OBJECT_KB} "
                                f"status=none 2>/dev/null || true",
                                f"dd if=/dev/zero of=/var/www/large.bin "
                                f"bs=1M count={HTTP_LARGE_OBJECT_MB} "
                                f"status=none 2>/dev/null || true",
                                "cd /var/www && nohup python3 -m http.server 80 "
                                ">/dev/null 2>&1 &",
                            ]
                        )

    def _wire_wan(self) -> None:
        for site in self.spec.sites.values():
            edge = self._edges[site.name]
            for provider in site.wan_providers:
                isp = self._isps[provider]
                link = f"{edge.name}_{isp.name}"
                self._connect(edge.name, isp.name, link)
                subnet = self._next_wan()
                # edge gets .1, isp gets .2 of the /30 usable (network+1, network+2)
                edge_cidr = str(
                    IPv4Interface(f"{subnet.network_address + 1}/{subnet.prefixlen}")
                )
                isp_cidr = str(
                    IPv4Interface(f"{subnet.network_address + 2}/{subnet.prefixlen}")
                )
                edge_ip = str(subnet.network_address + 1)
                isp_ip = str(subnet.network_address + 2)
                wan_dev = f"eth{edge.eth_index}"
                isp_dev = f"eth{isp.eth_index}"
                edge.cmd_list.append(f"ip addr add {edge_cidr} dev {wan_dev}")
                edge.eth_index += 1
                isp.cmd_list.append(f"ip addr add {isp_cidr} dev {isp_dev}")
                isp.eth_index += 1
                edge.wan[provider] = (edge_cidr, isp_ip, edge_ip, wan_dev)
                # Healthy regional WAN propagation (Cisco SD-WAN small-branch
                # case study). Applied on both ends of each PE attachment so
                # branch→HQ one-way ≈ 40 ms and RTT ≈ 80 ms.
                delay = UNDERLAY_ONE_WAY_DELAY_MS
                edge.cmd_list.append(
                    f"tc qdisc replace dev {wan_dev} root netem delay {delay}ms || true"
                )
                isp.cmd_list.append(
                    f"tc qdisc replace dev {isp_dev} root netem delay {delay}ms || true"
                )
                # GUEST NAT uses two routed /32 aliases.  They model a managed
                # NAT pool while keeping address withdrawal independent from
                # the provider-facing /30 that carries the underlay.
                if provider == site.wan_providers[0] and site.name == "br1":
                    for suffix in ("10", "11"):
                        public_ip = f"198.18.1.{suffix}"
                        edge.cmd_list.append(
                            f"ip addr add {public_ip}/32 dev {wan_dev}"
                        )
                        isp.cmd_list.append(
                            f"ip route add {public_ip}/32 via {edge_ip}"
                        )

    def _build_tunnels(self) -> None:
        for tspec in self.spec.tunnels:
            self._add_tunnel(tspec)

    def _add_tunnel(self, tspec: TunnelSpec) -> None:
        spoke_site = tspec.local_site
        hub_site = tspec.remote_site
        spoke = self._edges[spoke_site]
        hub = self._edges[hub_site]
        provider = tspec.provider
        if provider not in spoke.wan or provider not in hub.wan:
            raise RuntimeError(
                f"Tunnel {spoke_site}->{hub_site} via {provider} missing WAN attachment"
            )
        spoke_wan = spoke.wan[provider][2]
        hub_wan = hub.wan[provider][2]
        subnet = self._next_tun()
        # hub .1, spoke .2
        hub_tun = IPv4Interface(f"{subnet.network_address + 1}/30")
        spoke_tun = IPv4Interface(f"{subnet.network_address + 2}/30")
        spoke_iface = tspec.iface
        hub_iface = hub_iface_for(spoke_site, hub_site, backup=not tspec.primary)

        # Unique ListenPort per iface on each edge (HQ is both spoke and hub).
        hub_port = self._next_listen_port(hub_site)
        spoke_port = self._next_listen_port(spoke_site)

        spoke.tunnels[spoke_iface] = (
            str(spoke_tun),
            str(hub_tun.ip),
            spoke_port,
            hub_site,
            tspec.primary,
            tspec.local_pref,
            hub_wan,
        )
        hub.tunnels[hub_iface] = (
            str(hub_tun),
            str(spoke_tun.ip),
            hub_port,
            spoke_site,
            tspec.primary,
            tspec.local_pref,
            spoke_wan,
        )

        # Underlay host routes to remote WAN endpoints via matching ISP PE
        spoke_isp_gw = spoke.wan[provider][1]
        hub_isp_gw = hub.wan[provider][1]
        spoke.cmd_list.append(f"ip route add {hub_wan}/32 via {spoke_isp_gw}")
        hub.cmd_list.append(f"ip route add {spoke_wan}/32 via {hub_isp_gw}")

        self.built_tunnels.append(
            BuiltTunnel(
                spoke=spoke_site,
                hub=hub_site,
                provider=provider,
                primary=tspec.primary,
                local_pref=tspec.local_pref,
                spoke_iface=spoke_iface,
                hub_iface=hub_iface,
                spoke_tunnel_ip=str(spoke_tun.ip),
                hub_tunnel_ip=str(hub_tun.ip),
                spoke_wan_ip=spoke_wan,
                hub_wan_ip=hub_wan,
                listen_port_spoke=spoke_port,
                listen_port_hub=hub_port,
            )
        )

    def _write_edge_configs(self) -> None:
        for site_name, edge in self._edges.items():
            site = self.spec.sites[site_name]
            edge.cmd_list.insert(0, "sysctl -w net.ipv4.ip_forward=1")
            edge.cmd_list.insert(1, "sysctl -w net.ipv4.conf.all.rp_filter=0")
            edge.cmd_list.insert(2, "sysctl -w net.ipv4.conf.default.rp_filter=0")
            priv, _pub = self._key_pairs[edge.key_idx]

            # WireGuard interfaces (endpoint ports from BuiltTunnel)
            for bt in self.built_tunnels:
                if site_name == bt.spoke:
                    iface = bt.spoke_iface
                    local_cidr = edge.tunnels[iface][0]
                    listen_port = bt.listen_port_spoke
                    remote_site = bt.hub
                    remote_wan = bt.hub_wan_ip
                    peer_listen = bt.listen_port_hub
                elif site_name == bt.hub:
                    iface = bt.hub_iface
                    local_cidr = edge.tunnels[iface][0]
                    listen_port = bt.listen_port_hub
                    remote_site = bt.spoke
                    remote_wan = bt.spoke_wan_ip
                    peer_listen = bt.listen_port_spoke
                else:
                    continue
                remote_edge = self._edges[remote_site]
                _rpriv, rpub = self._key_pairs[remote_edge.key_idx]
                conf = render_wg_conf(
                    private_key=priv,
                    address_cidr=local_cidr,
                    listen_port=listen_port,
                    peer_public_key=rpub,
                    peer_endpoint=f"{remote_wan}:{peer_listen}",
                )
                edge.machine.create_file_from_string(
                    conf, f"/etc/wireguard/{iface}.conf"
                )
                edge.cmd_list.append(f"wg-quick up {iface}")
                # Healthy overlay egress QoS: EF vs BE classes; no DSCP rewrite.
                qos = overlay_qos_for(self.spec.size)
                edge.cmd_list.extend(overlay_qos_startup_cmds(iface, qos))

            # Local-only VRFs: default via primary ISP PE in default VRF + NAT.
            local_only = [lan for lan in site.lans if lan.role in LOCAL_ONLY_ROLES]
            if local_only:
                primary_wan = edge.wan[site.wan_providers[0]]
                primary_pe = primary_wan[1]
                primary_wan_dev = primary_wan[3]
                edge.cmd_list.extend(
                    [
                        "nft add table ip nat || true",
                        "nft 'add chain ip nat POSTROUTING { type nat hook postrouting priority 100 ; }' || true",
                        "nft add table ip filter || true",
                        "nft 'add chain ip filter FORWARD { type filter hook forward priority 0 ; policy accept; }' || true",
                    ]
                )
                for lan in local_only:
                    edge.cmd_list.extend(
                        (
                            f"ip route add vrf {lan.vrf} default via {primary_pe} "
                            f"dev {primary_wan_dev} || true",
                            # Conntrack applies reverse NAT before the main-table
                            # route lookup.  Point that lookup back into the
                            # local VRF so replies reach the guest interface.
                            f"ip route add {lan.prefix} dev {lan.vrf} || true",
                        )
                    )
                    edge.cmd_list.append(
                        f"nft add rule ip nat POSTROUTING ip saddr {lan.prefix} "
                        "snat to 198.18.1.10 || true"
                    )
                    edge.cmd_list.append(
                        f"nft add rule ip filter FORWARD ip saddr {lan.prefix} "
                        "ip daddr 10.0.0.0/8 drop || true"
                    )

            overlay_roles = tuple(
                lan.role for lan in site.lans if lan.role in OVERLAY_ROLES
            )
            local_adv = [lan.prefix for lan in site.lans if lan.advertise]
            peer_asns = {name: s.asn for name, s in self.spec.sites.items()}
            rid = next(iter(edge.wan.values()))[2]
            frr = _render_edge_frr(
                hostname=edge.name,
                asn=site.asn,
                router_id=rid,
                is_hub=site.is_hub,
                overlay_roles=overlay_roles,
                local_adv_prefixes=local_adv,
                tunnels=edge.tunnels,
                peer_asns=peer_asns,
            )
            edge.machine.create_file_from_path(
                str(pkg_path("net_env/utils/kathara/bgp/daemons")), "/etc/frr/daemons"
            )
            edge.machine.create_file_from_path(
                str(pkg_path("net_env/utils/kathara/bgp/vtysh.conf")),
                "/etc/frr/vtysh.conf",
            )
            edge.machine.create_file_from_string(frr, "/etc/frr/frr.conf")
            edge.cmd_list.append("service frr start")
            self.lab.create_file_from_list(edge.cmd_list, f"{edge.name}.startup")

    def _write_isp_configs(self) -> None:
        for isp in self._isps.values():
            isp.cmd_list.insert(0, "sysctl -w net.ipv4.ip_forward=1")
            isp.machine.create_file_from_string(PROVIDER_DAEMONS, "/etc/frr/daemons")
            isp.machine.create_file_from_path(
                str(pkg_path("net_env/utils/kathara/bgp/vtysh.conf")),
                "/etc/frr/vtysh.conf",
            )
            isp.machine.create_file_from_string(
                f"!\nhostname {isp.name}\n!\nlog file /var/log/frr/frr.log\n!\nline vty\n!\n",
                "/etc/frr/frr.conf",
            )
            isp.cmd_list.append("service frr start")
            self.lab.create_file_from_list(isp.cmd_list, f"{isp.name}.startup")

    def _write_host_configs(self) -> None:
        for host in self._hosts.values():
            self.lab.create_file_from_list(host.cmd_list, f"{host.name}.startup")

    def startup_verify_lab(self) -> dict:
        from nika.net_env.enterprise_branch.verify import (
            verify_enterprise_branch_lab_startup,
        )

        return verify_enterprise_branch_lab_startup(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            topo_size=self.topo_size,
            built_tunnels=self.built_tunnels,
            spec=self.spec,
        )

    def verify_lab(self) -> dict:
        from nika.net_env.enterprise_branch.verify import (
            verify_enterprise_branch_lab,
        )

        return verify_enterprise_branch_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            topo_size=self.topo_size,
            built_tunnels=self.built_tunnels,
            spec=self.spec,
        )
