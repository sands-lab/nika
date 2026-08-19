"""Enterprise Branch VPN / WAN: hub-and-spoke overlay over provider underlay."""

from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import IPv4Interface, IPv4Network

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase
from nika.net_env.kathara.enterprise_wan.enterprise_branch.addressing import (
    edge_name_for,
    isp_name_for,
    lan_edge_ip,
    lan_host_ip,
)
from nika.net_env.kathara.enterprise_wan.enterprise_branch.topology import (
    BuiltTunnel,
    TunnelSpec,
    TopoSpec,
    TopoSize,
    build_topo_spec,
    hub_iface_for,
)
from nika.net_env.kathara.enterprise_wan.enterprise_branch.wireguard import (
    load_key_pairs,
    render_wg_conf,
)

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

EDGE_BGP_TEMPLATE = """\
!
hostname {hostname}
!
log file /var/log/frr/frr.log
!
ip prefix-list ENTERPRISE seq 5 permit 10.0.0.0/8 le 24
!
{local_prefix_lists}\
route-map RM-OUT-LOCAL permit 10
 match ip address prefix-list LOCAL-ADV
!
route-map RM-OUT-TRANSIT permit 10
 match ip address prefix-list ENTERPRISE
!
{inbound_maps}\
router bgp {asn}
 bgp router-id {router_id}
 no bgp ebgp-requires-policy
{networks}\
{neighbors}\
!
line vty
!
"""


@dataclass
class EdgeRuntime:
    name: str
    site: str
    machine: Machine
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    # provider -> (local_wan_cidr, isp_pe_ip, local_wan_ip)
    wan: dict[str, tuple[str, str, str]] = field(default_factory=dict)
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


class EnterpriseBranch(NetworkEnvBase):
    LAB_NAME = "enterprise_branch"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = ["arp", "link", "mac", "icmp", "frr", "bgp", "pc", "http", "vpn"]
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
        self._hub_listen_ports: dict[str, int] = {}

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
            "authorized business prefixes. Providers only forward tunnel "
            "endpoint reachability."
        )

    def _next_wan(self) -> IPv4Network:
        return self._wan_pool.pop(0)

    def _next_tun(self) -> IPv4Network:
        return self._tun_pool.pop(0)

    def _hub_port(self, hub_site: str) -> int:
        port = self._hub_listen_ports.get(hub_site, 51820)
        self._hub_listen_ports[hub_site] = port + 1
        return port

    def _create_nodes(self) -> None:
        key_i = 0
        for site_name in self.spec.sites:
            ename = edge_name_for(site_name)
            machine = self.lab.new_machine(
                ename, **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"}
            )
            self._edges[site_name] = EdgeRuntime(
                name=ename, site=site_name, machine=machine, key_idx=key_i
            )
            key_i += 1

        for provider in self.spec.providers:
            iname = isp_name_for(provider)
            machine = self.lab.new_machine(
                iname, **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"}
            )
            self._isps[provider] = IspRuntime(
                name=iname, provider=provider, machine=machine
            )

        for site in self.spec.sites.values():
            for lan in site.lans:
                image = (
                    "kathara/nika-base" if lan.role != "server" else "kathara/nika-base"
                )
                machine = self.lab.new_machine(
                    lan.host_name,
                    **{"image": image, "cpus": 0.5, "mem": "256m"},
                )
                self._hosts[lan.host_name] = HostRuntime(
                    name=lan.host_name,
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
            for lan in site.lans:
                host = self._hosts[lan.host_name]
                link = f"{edge.name}_{lan.host_name}"
                self._connect(edge.name, lan.host_name, link)
                e_ip = lan_edge_ip(site.site_id, lan.role)
                h_ip = lan_host_ip(site.site_id, lan.role)
                edge.cmd_list.append(f"ip addr add {e_ip} dev eth{edge.eth_index}")
                edge.eth_index += 1
                host.cmd_list.append(f"ip addr add {h_ip} dev eth0")
                host.cmd_list.append(f"ip route add default via {e_ip.ip} dev eth0")
                host.ip = str(h_ip.ip)
                if lan.role == "server":
                    host.cmd_list.append(
                        "nohup python3 -m http.server 80 >/dev/null 2>&1 &"
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
                edge.cmd_list.append(f"ip addr add {edge_cidr} dev eth{edge.eth_index}")
                edge.eth_index += 1
                isp.cmd_list.append(f"ip addr add {isp_cidr} dev eth{isp.eth_index}")
                isp.eth_index += 1
                edge.wan[provider] = (edge_cidr, isp_ip, edge_ip)

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
        if hub_site == "dc2":
            hub_iface = f"wg_{spoke_site}_dc2"
        elif tspec.primary:
            hub_iface = hub_iface_for(spoke_site, hub_site, backup=False)
        else:
            hub_iface = hub_iface_for(spoke_site, hub_site, backup=True)

        hub_port = self._hub_port(hub_site)
        spoke_port = tspec.listen_port

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

            # Guest NAT + block guest from enterprise overlay (local Internet only)
            if any(lan.role == "guest" for lan in site.lans):
                edge.cmd_list.extend(
                    [
                        "nft add table ip nat || true",
                        "nft 'add chain ip nat POSTROUTING { type nat hook postrouting priority 100 ; }' || true",
                        "nft add table ip filter || true",
                        "nft 'add chain ip filter FORWARD { type filter hook forward priority 0 ; policy accept; }' || true",
                    ]
                )
                for lan in site.lans:
                    if lan.role != "guest":
                        continue
                    edge.cmd_list.append(
                        f"nft add rule ip nat POSTROUTING ip saddr {lan.prefix} masquerade || true"
                    )
                    edge.cmd_list.append(
                        f"nft add rule ip filter FORWARD ip saddr {lan.prefix} ip daddr 10.0.0.0/8 drop || true"
                    )

            # FRR BGP
            networks = ""
            local_pl = ""
            seq = 5
            for lan in site.lans:
                if not lan.advertise:
                    continue
                networks += f" network {lan.prefix}\n"
                local_pl += f"ip prefix-list LOCAL-ADV seq {seq} permit {lan.prefix}\n"
                seq += 5
            if not local_pl:
                local_pl = "ip prefix-list LOCAL-ADV seq 5 deny 0.0.0.0/0 le 32\n"

            inbound_maps = ""
            neighbors = ""
            out_map = "RM-OUT-TRANSIT" if site.is_hub else "RM-OUT-LOCAL"
            for iface, (
                local_cidr,
                peer_tun_ip,
                _port,
                remote_site,
                _primary,
                local_pref,
                _rwan,
            ) in edge.tunnels.items():
                remote_asn = self.spec.sites[remote_site].asn
                local_ip = local_cidr.split("/")[0]
                map_name = f"RM-IN-{iface}"
                inbound_maps += (
                    f"route-map {map_name} permit 10\n"
                    f" match ip address prefix-list ENTERPRISE\n"
                    f" set local-preference {local_pref}\n"
                    "!\n"
                )
                neighbors += (
                    f" neighbor {peer_tun_ip} remote-as {remote_asn}\n"
                    f" neighbor {peer_tun_ip} update-source {local_ip}\n"
                    f" neighbor {peer_tun_ip} route-map {out_map} out\n"
                    f" neighbor {peer_tun_ip} route-map {map_name} in\n"
                )
                if site.is_hub:
                    neighbors += f" neighbor {peer_tun_ip} next-hop-self\n"

            rid = next(iter(edge.wan.values()))[2]
            frr = EDGE_BGP_TEMPLATE.format(
                hostname=edge.name,
                local_prefix_lists=local_pl,
                inbound_maps=inbound_maps,
                asn=site.asn,
                router_id=rid,
                networks=networks,
                neighbors=neighbors,
            )
            edge.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/bgp/daemons")), "/etc/frr/daemons"
            )
            edge.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/bgp/vtysh.conf")),
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
                str(pkg_path("net_env/kathara/utils/bgp/vtysh.conf")),
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

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.enterprise_wan.enterprise_branch.verify import (
            verify_enterprise_branch_lab,
        )

        return verify_enterprise_branch_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            topo_size=self.topo_size,
            built_tunnels=self.built_tunnels,
            spec=self.spec,
        )
