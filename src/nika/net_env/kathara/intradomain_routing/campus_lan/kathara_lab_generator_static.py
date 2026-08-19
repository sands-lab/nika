#!/usr/bin/env python3
"""
Standalone lab generator for OSPF Enterprise Static topology.
Generates Kathara-compatible lab configuration WITHOUT Kathara dependency.
Output: topology/ folder containing lab.conf, *.startup, and host config subfolders.
"""

import os
import textwrap
from dataclasses import dataclass, field
from ipaddress import IPv4Interface, IPv4Network
from typing import Literal

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_DIR))))
)
OSPF_UTILS = os.path.join(PROJECT_ROOT, "src/nika/net_env/kathara/utils/ospf")


def assign_p2p_ips(subnet: IPv4Network) -> tuple[str, str]:
    base = subnet.network_address
    ip0 = IPv4Interface(f"{base}/31")
    ip1 = IPv4Interface(f"{base + 1}/31")
    return str(ip0), str(ip1)


FRR_BASE_TEMPLATE = """
!
! FRRouting configuration file
!
!
!  OSPF CONFIGURATION
!
router ospf
 router-id {router_id}
 {ospf_networks}
!
!
log file /var/log/frr/frr.log
"""


@dataclass
class RouterMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    router_id: str = ""
    frr_ospf_configs: list[str] = field(default_factory=list)
    image: str = "kathara/nika-frr"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)
    extra_files: dict[str, str] = field(default_factory=dict)


@dataclass
class SwitchMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    host_network: IPv4Network | None = None
    image: str = "kathara/nika-base"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class HostMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    ip_address: str | None = None
    image: str = "kathara/nika-base"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)
    extra_files: dict[str, str] = field(default_factory=dict)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def generate_ospf_enterprise_static_topology(
    topo_size: Literal["s", "m", "l"] = "s",
    output_dir: str | None = None,
    ospf_utils_path: str | None = None,
) -> str:
    """
    Generate Kathara-compatible lab configuration for OSPF Enterprise Static.
    Returns the absolute path to the output directory.
    """
    if output_dir is None:
        output_dir = os.path.join(SCRIPT_DIR, "topology")
    ospf_utils = ospf_utils_path or OSPF_UTILS

    match topo_size:
        case "s":
            DIST_SW_COUNT, ACCESS_SW_PER_DIST, HOST_PER_ACCESS = 1, 1, 1
        case "m":
            DIST_SW_COUNT, ACCESS_SW_PER_DIST, HOST_PER_ACCESS = 2, 2, 2
        case "l":
            DIST_SW_COUNT, ACCESS_SW_PER_DIST, HOST_PER_ACCESS = 4, 4, 4
        case _:
            raise ValueError("topo_size should be 's', 'm', or 'l'.")

    core_routers: dict[int, RouterMeta] = {}
    for core_id in range(1, 4):
        core_routers[core_id] = RouterMeta(name=f"router_core_{core_id}")

    core_dists: dict[int, list[RouterMeta]] = {}
    dist_accesses: dict[str, list[SwitchMeta]] = {}
    access_hosts: dict[str, list[HostMeta]] = {}
    for core_id in range(1, 3):
        core_dists[core_id] = []
        for dist_id in range(1, DIST_SW_COUNT + 1):
            dist_name = f"switch_dist_{core_id}_{dist_id}"
            core_dists[core_id].append(RouterMeta(name=dist_name))
            dist_key = f"{core_id}_{dist_id}"
            dist_accesses[dist_key] = []
            for access_id in range(1, ACCESS_SW_PER_DIST + 1):
                access_name = f"switch_access_{core_id}_{dist_id}_{access_id}"
                dist_accesses[dist_key].append(SwitchMeta(name=access_name))
                access_key = f"{core_id}_{dist_id}_{access_id}"
                access_hosts[access_key] = []
                for host_id in range(1, HOST_PER_ACCESS + 1):
                    access_hosts[access_key].append(
                        HostMeta(name=f"pc_{core_id}_{dist_id}_{access_id}_{host_id}")
                    )

    server_network = IPv4Network("10.200.0.0/24")
    server_ip_gen = list(server_network.hosts())
    server_gateway_ip = server_ip_gen.pop(0)
    servers: dict[str, HostMeta] = {}
    tot_dns: list[HostMeta] = []
    web_servers: list[HostMeta] = []
    dns_meta = HostMeta(name="dns_server")
    servers["dns_server"] = dns_meta
    tot_dns.append(dns_meta)
    for web_idx in range(4):
        web_meta = HostMeta(name=f"web_server_{web_idx}")
        servers[f"web_server_{web_idx}"] = web_meta
        web_servers.append(web_meta)
    server_access_meta = RouterMeta(name="switch_server_access")

    subnets_infra = list(IPv4Network("172.16.0.0/16").subnets(new_prefix=31))

    for core_id1 in range(1, 3):
        for core_id2 in range(core_id1 + 1, 4):
            core_meta1 = core_routers[core_id1]
            core_meta2 = core_routers[core_id2]
            link_name = f"{core_meta1.name}_{core_meta2.name}"
            core_meta1.links.append((core_meta1.eth_index, link_name))
            core_meta2.links.append((core_meta2.eth_index, link_name))
            subnet = subnets_infra.pop(0)
            a_ip, b_ip = assign_p2p_ips(subnet)
            core_meta1.cmd_list.append(
                f"ip addr add {a_ip} dev eth{core_meta1.eth_index}"
            )
            core_meta2.cmd_list.append(
                f"ip addr add {b_ip} dev eth{core_meta2.eth_index}"
            )
            core_meta1.frr_ospf_configs.append(f"network {subnet} area 0")
            core_meta2.frr_ospf_configs.append(f"network {subnet} area 0")
            if core_meta1.router_id == "":
                core_meta1.router_id = a_ip.split("/")[0]
            if core_meta2.router_id == "":
                core_meta2.router_id = b_ip.split("/")[0]
            core_meta1.eth_index += 1
            core_meta2.eth_index += 1

    for core_id in range(1, 3):
        core_meta = core_routers[core_id]
        for dist_meta in core_dists[core_id]:
            link_name = f"{core_meta.name}_{dist_meta.name}"
            core_meta.links.append((core_meta.eth_index, link_name))
            dist_meta.links.append((dist_meta.eth_index, link_name))
            subnet = subnets_infra.pop(0)
            a_ip, b_ip = assign_p2p_ips(subnet)
            core_meta.cmd_list.append(
                f"ip addr add {a_ip} dev eth{core_meta.eth_index}"
            )
            core_meta.eth_index += 1
            dist_meta.cmd_list.append(
                f"ip addr add {b_ip} dev eth{dist_meta.eth_index}"
            )
            core_meta.frr_ospf_configs.append(f"network {subnet} area 1")
            dist_meta.frr_ospf_configs.append(f"network {subnet} area 1")
            if core_meta.router_id == "":
                core_meta.router_id = a_ip.split("/")[0]
            if dist_meta.router_id == "":
                dist_meta.router_id = b_ip.split("/")[0]
            dist_meta.eth_index += 1

    for core_id in range(1, 3):
        for dist_id in range(1, DIST_SW_COUNT + 1):
            dist_key = f"{core_id}_{dist_id}"
            dist_meta = core_dists[core_id][dist_id - 1]
            dist_meta.cmd_list.append("brctl addbr br0")
            dist_meta.cmd_list.append("ip link set br0 up")
            for access_meta in dist_accesses[dist_key]:
                link_name = f"{dist_meta.name}_{access_meta.name}"
                dist_meta.links.append((dist_meta.eth_index, link_name))
                access_meta.links.append((access_meta.eth_index, link_name))
                dist_meta.cmd_list.append(f"brctl addif br0 eth{dist_meta.eth_index}")
                access_meta.cmd_list.append("brctl addbr br0")
                access_meta.cmd_list.append("ip link set br0 up")
                access_meta.cmd_list.append(
                    f"brctl addif br0 eth{access_meta.eth_index}"
                )
                dist_meta.eth_index += 1
                access_meta.eth_index += 1

    for core_id in range(1, 3):
        for dist_id in range(1, DIST_SW_COUNT + 1):
            dist_network = IPv4Network(f"10.{core_id}.{dist_id}.0/24")
            host_ip_gen = list(dist_network.hosts())
            dist_key = f"{core_id}_{dist_id}"
            dist_meta = core_dists[core_id][dist_id - 1]
            dist_meta.frr_ospf_configs.append(f"network {dist_network} area 1")
            default_gateway_ip = host_ip_gen.pop(0)
            dist_meta.cmd_list.append(
                f"ip addr add {default_gateway_ip}/{dist_network.prefixlen} dev br0"
            )
            for access_id in range(1, ACCESS_SW_PER_DIST + 1):
                access_key = f"{core_id}_{dist_id}_{access_id}"
                access_meta = dist_accesses[dist_key][access_id - 1]
                access_meta.host_network = dist_network
                for host_meta in access_hosts[access_key]:
                    link_name = f"{access_meta.name}_{host_meta.name}"
                    access_meta.links.append((access_meta.eth_index, link_name))
                    host_meta.links.append((0, link_name))
                    host_ip = host_ip_gen.pop(0)
                    host_meta.cmd_list.append(
                        f"ip addr add {host_ip}/{dist_network.prefixlen} dev eth{host_meta.eth_index}"
                    )
                    host_meta.ip_address = str(host_ip)
                    host_meta.cmd_list.append(
                        f"ip route add default via {default_gateway_ip} dev eth{host_meta.eth_index}"
                    )
                    host_meta.eth_index += 1
                    access_meta.cmd_list.append(
                        f"brctl addif br0 eth{access_meta.eth_index}"
                    )
                    access_meta.eth_index += 1

    server_access_meta.cmd_list.append("brctl addbr br0")
    server_access_meta.cmd_list.append("ip link set br0 up")
    server_access_meta.frr_ospf_configs.append(f"network {server_network} area 0")
    server_access_meta.cmd_list.append(
        f"ip addr add {server_gateway_ip}/{server_network.prefixlen} dev br0"
    )
    for server_name, server_meta in servers.items():
        link_name = f"{server_access_meta.name}_{server_meta.name}"
        server_access_meta.links.append((server_access_meta.eth_index, link_name))
        server_meta.links.append((0, link_name))
        server_access_meta.cmd_list.append(
            f"brctl addif br0 eth{server_access_meta.eth_index}"
        )
        server_ip = server_ip_gen.pop(0)
        server_meta.cmd_list.append(
            f"ip addr add {server_ip}/{server_network.prefixlen} dev eth{server_meta.eth_index}"
        )
        server_meta.ip_address = str(server_ip)
        server_meta.cmd_list.append(
            f"ip route add default via {server_gateway_ip} dev eth{server_meta.eth_index}"
        )
        server_meta.eth_index += 1
        server_access_meta.eth_index += 1

    core3_meta = core_routers[3]
    link_name = f"{core3_meta.name}_{server_access_meta.name}"
    core3_meta.links.append((core3_meta.eth_index, link_name))
    server_access_meta.links.append((server_access_meta.eth_index, link_name))
    subnet = subnets_infra.pop(0)
    a_ip, b_ip = assign_p2p_ips(subnet)
    core3_meta.cmd_list.append(f"ip addr add {a_ip} dev eth{core3_meta.eth_index}")
    server_access_meta.cmd_list.append(
        f"ip addr add {b_ip} dev eth{server_access_meta.eth_index}"
    )
    core3_meta.frr_ospf_configs.append(f"network {subnet} area 0")
    server_access_meta.frr_ospf_configs.append(f"network {subnet} area 0")
    if core3_meta.router_id == "":
        core3_meta.router_id = a_ip.split("/")[0]
    if server_access_meta.router_id == "":
        server_access_meta.router_id = b_ip.split("/")[0]
    core3_meta.eth_index += 1
    server_access_meta.eth_index += 1

    daemons_content = _read_file(os.path.join(ospf_utils, "daemons"))
    vtysh_content = _read_file(os.path.join(ospf_utils, "vtysh.conf"))
    for core_meta in core_routers.values():
        core_meta.extra_files["/etc/frr/daemons"] = daemons_content
        core_meta.extra_files["/etc/frr/vtysh.conf"] = vtysh_content
        core_meta.extra_files["/etc/frr/frr.conf"] = FRR_BASE_TEMPLATE.format(
            router_id=core_meta.router_id,
            ospf_networks="\n ".join(core_meta.frr_ospf_configs),
        )
        core_meta.cmd_list.append("service frr start")
    for dist_metas in core_dists.values():
        for dist_meta in dist_metas:
            dist_meta.extra_files["/etc/frr/daemons"] = daemons_content
            dist_meta.extra_files["/etc/frr/vtysh.conf"] = vtysh_content
            dist_meta.extra_files["/etc/frr/frr.conf"] = FRR_BASE_TEMPLATE.format(
                router_id=dist_meta.router_id,
                ospf_networks="\n ".join(dist_meta.frr_ospf_configs),
            )
            dist_meta.cmd_list.append("service frr start")
    server_access_meta.extra_files["/etc/frr/daemons"] = daemons_content
    server_access_meta.extra_files["/etc/frr/vtysh.conf"] = vtysh_content
    server_access_meta.extra_files["/etc/frr/frr.conf"] = FRR_BASE_TEMPLATE.format(
        router_id=server_access_meta.router_id,
        ospf_networks="\n ".join(server_access_meta.frr_ospf_configs),
    )
    server_access_meta.cmd_list.append("service frr start")

    for host_metas in access_hosts.values():
        for host_meta in host_metas:
            host_meta.extra_files["/etc/resolv.conf"] = "".join(
                f"nameserver {d.ip_address}\n" for d in tot_dns
            )
            host_meta.cmd_list.append("dhclient eth0")

    zone_name = "local"
    ns_name = "ns1"
    dns_meta.extra_files["/etc/bind/named.conf"] = textwrap.dedent(
        f"""\
        options {{
            directory "/var/cache/bind";
            listen-on port 53 {{ any; }};
            allow-query     {{ any; }};
            recursion no;
        }};

        zone "{zone_name}" IN {{
            type master;
            file "/etc/bind/db.{zone_name}";
        }};"""
    )
    basic_bind = textwrap.dedent(
        f"""\
        $TTL 1H
        @   IN  SOA {ns_name}.{zone_name}. admin.{zone_name}. (
                2025111101 ; Serial
                1H         ; Refresh
                15M        ; Retry
                1W         ; Expire
                1D )       ; Minimum

            IN  NS  {ns_name}.{zone_name}.
        {ns_name} IN  A   {dns_meta.ip_address}
        """
    )
    for web_idx, web in enumerate(web_servers):
        basic_bind += f"web{web_idx} IN  A  {web.ip_address}\n"
    dns_meta.extra_files[f"/etc/bind/db.{zone_name}"] = basic_bind
    dns_meta.cmd_list.append("systemctl start named")

    for web_idx, web_meta in enumerate(web_servers):
        web_meta.extra_files["/var/www/html/index.html"] = (
            f"<html><body><h1>Welcome to Web Server {web_idx}</h1></body></html>\n"
        )
        web_meta.cmd_list.append("service apache2 start")

    all_machines: list[RouterMeta | SwitchMeta | HostMeta] = (
        list(core_routers.values())
        + [d for dists in core_dists.values() for d in dists]
        + [server_access_meta]
        + [a for access_list in dist_accesses.values() for a in access_list]
        + [h for host_list in access_hosts.values() for h in host_list]
        + list(servers.values())
    )

    out_path = os.path.abspath(output_dir)
    os.makedirs(out_path, exist_ok=True)
    lab_conf_lines = [
        'LAB_NAME="ospf_enterprise_static"',
        'LAB_DESCRIPTION="OSPF Enterprise Static - generated by kathara_lab_generator_static"',
        "",
    ]
    for meta in all_machines:
        for eth_idx, cd in meta.links:
            lab_conf_lines.append(f'{meta.name}[{eth_idx}]="{cd}"')
        lab_conf_lines.append(f'{meta.name}[image]="{meta.image}"')
        lab_conf_lines.append(f"{meta.name}[cpus]={meta.cpus}")
        lab_conf_lines.append(f'{meta.name}[mem]="{meta.mem}"')
        lab_conf_lines.append("")
    with open(os.path.join(out_path, "lab.conf"), "w", encoding="utf-8") as f:
        f.write("\n".join(lab_conf_lines))
    for meta in all_machines:
        with open(
            os.path.join(out_path, f"{meta.name}.startup"), "w", encoding="utf-8"
        ) as f:
            f.write("\n".join(meta.cmd_list))
        if hasattr(meta, "extra_files") and meta.extra_files:
            host_dir = os.path.join(out_path, meta.name)
            for file_path, content in meta.extra_files.items():
                rel_path = file_path.lstrip("/")
                full_path = os.path.join(host_dir, rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)
    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Kathara lab config for OSPF Enterprise Static"
    )
    parser.add_argument("-s", "--size", choices=["s", "m", "l"], default="s")
    parser.add_argument("-o", "--output", default=None)
    args = parser.parse_args()
    out = generate_ospf_enterprise_static_topology(
        topo_size=args.size,
        output_dir=args.output or os.path.join(SCRIPT_DIR, "topology"),
    )
    print(f"Lab configuration generated at: {out}")
