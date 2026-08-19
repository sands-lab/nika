#!/usr/bin/env python3
"""
Standalone lab generator for DC Clos Service topology.
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
BGP_UTILS = os.path.join(PROJECT_ROOT, "src/nika/net_env/kathara/utils/bgp")


# P2P links use 172.16.0.0/16 with /31 per link.
# Host access networks use 10.<pod>.<leaf>.0/24 with leaf .1 and host .2
def assign_p2p_ips(subnet: IPv4Network) -> tuple[str, str]:
    base = subnet.network_address
    ip0 = IPv4Interface(f"{base}/31")
    ip1 = IPv4Interface(f"{base + 1}/31")
    return str(ip0), str(ip1)


FRR_BASE_TEMPLATE = """
!
hostname {hostname}
!
log file /var/log/frr/frr.log
!
debug bgp keepalives
debug bgp updates in
debug bgp updates out
!
router bgp {AS_number}
 bgp router-id {router_id}
 bgp bestpath as-path multipath-relax
 maximum-paths 32
 no bgp ebgp-requires-policy
 {network} {neighbor_add_configs}!
line vty
"""

FRR_NEIGHBOR_ADD_TEMPLATE = """neighbor {neighbor_ip} remote-as {neighbor_as}
"""


@dataclass
class RouterMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    AS_number: int = 0
    router_id: str = ""
    frr_neighbor_configs: list[str] = field(default_factory=list)
    host_network: str | None = None
    image: str = "nika/frr"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(
        default_factory=list
    )  # (eth_idx, collision_domain)
    extra_files: dict[str, str] = field(default_factory=dict)  # path -> content


@dataclass
class HostMeta:
    name: str
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    ip_address: str | None = None
    image: str = "nika/base"
    cpus: float = 0.5
    mem: str = "256m"
    links: list[tuple[int, str]] = field(default_factory=list)
    extra_files: dict[str, str] = field(default_factory=dict)


def _read_file(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def generate_dc_clos_service_topology(
    topo_size: Literal["s", "m", "l"] = "s",
    output_dir: str | None = None,
    bgp_utils_path: str | None = None,
) -> str:
    """
    Generate Kathara-compatible lab configuration for DC Clos Service topology.
    Returns the absolute path to the output directory.
    Output defaults to topology/ directory next to this script.
    """
    if output_dir is None:
        output_dir = os.path.join(SCRIPT_DIR, "topology")
    bgp_utils = bgp_utils_path or BGP_UTILS

    if topo_size == "s":
        super_spine_count = 1
        spine_count = 2
        leaf_count = 2
    elif topo_size == "m":
        super_spine_count = 2
        spine_count = 4
        leaf_count = 4
    elif topo_size == "l":
        super_spine_count = 4
        spine_count = 8
        leaf_count = 8
    else:
        raise ValueError("Invalid topo_size. Choose from 's', 'm', 'l'.")

    pod_spines: dict[int, list[RouterMeta]] = {}
    pod_leaves: dict[int, list[RouterMeta]] = {}
    pod_dns: dict[int, list[HostMeta]] = {}
    pod_webservers: dict[int, list[HostMeta]] = {}

    tot_super_spines: list[RouterMeta] = []
    tot_spines: list[RouterMeta] = []
    tot_leaves: list[RouterMeta] = []
    tot_dns: list[HostMeta] = []
    tot_webservers: list[HostMeta] = []
    tot_clients: list[HostMeta] = []

    infra_pool = IPv4Network("172.16.0.0/16")
    subnets31 = list(infra_pool.subnets(new_prefix=31))

    # Create super spines
    for ss in range(super_spine_count):
        ss_name = f"super_spine_router_{ss}"
        router_ss_meta = RouterMeta(name=ss_name, AS_number=65000)
        tot_super_spines.append(router_ss_meta)

    # Create spines, leaves, dns, webservers per pod
    for pod in range(super_spine_count):
        pod_spines[pod] = []
        for spine_id in range(spine_count):
            spine_name = f"spine_router_{pod}_{spine_id}"
            spine_meta = RouterMeta(
                name=spine_name, AS_number=65100 + 10 * pod + spine_id
            )
            pod_spines[pod].append(spine_meta)
            tot_spines.append(spine_meta)

        pod_leaves[pod] = []
        for leaf_id in range(leaf_count):
            leaf_name = f"leaf_router_{pod}_{leaf_id}"
            leaf_meta = RouterMeta(name=leaf_name, AS_number=65200 + 10 * pod + leaf_id)
            pod_leaves[pod].append(leaf_meta)
            tot_leaves.append(leaf_meta)

        pod_dns[pod] = []
        dns_name = f"dns_pod{pod}"
        dns_meta = HostMeta(name=dns_name)
        pod_dns[pod].append(dns_meta)
        tot_dns.append(dns_meta)

        pod_webservers[pod] = []
        for host in range(leaf_count - 1):
            web_name = f"webserver{host}_pod{pod}"
            web_meta = HostMeta(name=web_name)
            pod_webservers[pod].append(web_meta)
            tot_webservers.append(web_meta)

    # Create client hosts
    for client_id in range(super_spine_count):
        client_name = f"client_{client_id}"
        client_meta = HostMeta(name=client_name)
        tot_clients.append(client_meta)

    # Links: super spines <-> spines
    for pod in range(super_spine_count):
        super_spine_meta = tot_super_spines[pod]
        for spine_meta in tot_spines:
            link_name = f"{super_spine_meta.name}_{spine_meta.name}"
            super_spine_meta.links.append((super_spine_meta.eth_index, link_name))
            spine_meta.links.append((spine_meta.eth_index, link_name))

            subnet = subnets31.pop(0)
            a_ip, b_ip = assign_p2p_ips(subnet)
            super_spine_meta.cmd_list.append(
                f"ip addr add {a_ip} dev eth{super_spine_meta.eth_index}"
            )
            super_spine_meta.eth_index += 1
            spine_meta.cmd_list.append(
                f"ip addr add {b_ip} dev eth{spine_meta.eth_index}"
            )
            spine_meta.eth_index += 1

            super_spine_meta.frr_neighbor_configs.append(
                FRR_NEIGHBOR_ADD_TEMPLATE.format(
                    neighbor_ip=b_ip.split("/")[0],
                    neighbor_as=spine_meta.AS_number,
                )
            )
            spine_meta.frr_neighbor_configs.append(
                FRR_NEIGHBOR_ADD_TEMPLATE.format(
                    neighbor_ip=a_ip.split("/")[0],
                    neighbor_as=super_spine_meta.AS_number,
                )
            )
            if super_spine_meta.router_id == "":
                super_spine_meta.router_id = a_ip.split("/")[0]
            if spine_meta.router_id == "":
                spine_meta.router_id = b_ip.split("/")[0]

    # Links: spines <-> leaves
    for pod in range(super_spine_count):
        for spine_meta in pod_spines[pod]:
            for leaf_meta in pod_leaves[pod]:
                link_name = f"{spine_meta.name}_{leaf_meta.name}"
                spine_meta.links.append((spine_meta.eth_index, link_name))
                leaf_meta.links.append((leaf_meta.eth_index, link_name))

                subnet = subnets31.pop(0)
                a_ip, b_ip = assign_p2p_ips(subnet)
                spine_meta.cmd_list.append(
                    f"ip addr add {a_ip} dev eth{spine_meta.eth_index}"
                )
                spine_meta.eth_index += 1
                leaf_meta.cmd_list.append(
                    f"ip addr add {b_ip} dev eth{leaf_meta.eth_index}"
                )
                leaf_meta.eth_index += 1

                spine_meta.frr_neighbor_configs.append(
                    FRR_NEIGHBOR_ADD_TEMPLATE.format(
                        neighbor_ip=b_ip.split("/")[0],
                        neighbor_as=leaf_meta.AS_number,
                    )
                )
                leaf_meta.frr_neighbor_configs.append(
                    FRR_NEIGHBOR_ADD_TEMPLATE.format(
                        neighbor_ip=a_ip.split("/")[0],
                        neighbor_as=spine_meta.AS_number,
                    )
                )
                if spine_meta.router_id == "":
                    spine_meta.router_id = a_ip.split("/")[0]
                if leaf_meta.router_id == "":
                    leaf_meta.router_id = b_ip.split("/")[0]

    # Links: leaves <-> internal hosts (dns, webserver)
    for pod in range(super_spine_count):
        pod_services = pod_dns[pod] + pod_webservers[pod]
        for idx in range(leaf_count):
            leaf_meta = pod_leaves[pod][idx]
            host = pod_services[idx]
            link_name = f"{leaf_meta.name}_{host.name}"
            leaf_meta.links.append((leaf_meta.eth_index, link_name))
            host.links.append((host.eth_index, link_name))

            subnet = IPv4Network(f"10.{pod}.{idx}.0/24")
            leaf_ip = IPv4Interface(f"{subnet.network_address + 1}/{subnet.prefixlen}")
            host_ip = IPv4Interface(f"{subnet.network_address + 2}/{subnet.prefixlen}")
            leaf_meta.cmd_list.append(
                f"ip addr add {leaf_ip} dev eth{leaf_meta.eth_index}"
            )
            leaf_meta.eth_index += 1
            host.cmd_list.append(f"ip addr add {host_ip} dev eth{host.eth_index}")
            host.cmd_list.append(
                f"ip route add default via {leaf_ip.ip} dev eth{host.eth_index}"
            )
            host.eth_index += 1
            leaf_meta.host_network = str(subnet)
            host.ip_address = str(host_ip.ip)

    # Links: super spine <-> client
    for pod in range(super_spine_count):
        client_meta = tot_clients[pod]
        super_spine_meta = tot_super_spines[pod]
        link_name = f"{super_spine_meta.name}_{client_meta.name}"
        super_spine_meta.links.append((super_spine_meta.eth_index, link_name))
        client_meta.links.append((client_meta.eth_index, link_name))

        subnet = IPv4Network(f"192.168.{pod}.0/24")
        ss_ip = IPv4Interface(f"{subnet.network_address + 1}/{subnet.prefixlen}")
        client_ip = IPv4Interface(f"{subnet.network_address + 2}/{subnet.prefixlen}")
        super_spine_meta.cmd_list.append(
            f"ip addr add {ss_ip} dev eth{super_spine_meta.eth_index}"
        )
        super_spine_meta.eth_index += 1
        client_meta.cmd_list.append(
            f"ip addr add {client_ip} dev eth{client_meta.eth_index}"
        )
        client_meta.cmd_list.append(
            f"ip route add default via {ss_ip.ip} dev eth{client_meta.eth_index}"
        )
        client_meta.eth_index += 1
        super_spine_meta.host_network = str(subnet)
        client_meta.ip_address = str(client_ip.ip)

    # Router configs: spines (no host network)
    daemons_content = _read_file(os.path.join(bgp_utils, "daemons"))
    vtysh_content = _read_file(os.path.join(bgp_utils, "vtysh.conf"))

    for router_meta in tot_spines:
        router_meta.extra_files["/etc/frr/daemons"] = daemons_content
        router_meta.extra_files["/etc/frr/vtysh.conf"] = vtysh_content
        frr_config = FRR_BASE_TEMPLATE.format(
            hostname=router_meta.name,
            AS_number=router_meta.AS_number,
            router_id=router_meta.router_id,
            network="",
            neighbor_add_configs=" ".join(router_meta.frr_neighbor_configs),
        )
        router_meta.extra_files["/etc/frr/frr.conf"] = frr_config
        router_meta.cmd_list.append("service frr start")

    # Router configs: leaves and super spines (with host network)
    for router_meta in tot_leaves + tot_super_spines:
        router_meta.extra_files["/etc/frr/daemons"] = daemons_content
        router_meta.extra_files["/etc/frr/vtysh.conf"] = vtysh_content
        frr_config = FRR_BASE_TEMPLATE.format(
            hostname=router_meta.name,
            AS_number=router_meta.AS_number,
            router_id=router_meta.router_id,
            network=f"network {router_meta.host_network}\n",
            neighbor_add_configs=" ".join(router_meta.frr_neighbor_configs),
        )
        router_meta.extra_files["/etc/frr/frr.conf"] = frr_config
        router_meta.cmd_list.append("service frr start")

    # Client configs
    for host in tot_clients:
        ns_content = "".join(f"nameserver {dns.ip_address}\n" for dns in tot_dns)
        host.extra_files["/etc/resolv.conf"] = ns_content

    # DNS configs
    for dns_idx, dns in enumerate(tot_dns):
        zone_name = f"pod{dns_idx}"
        ns_name = f"ns{dns_idx}"
        name_config = textwrap.dedent(
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
        dns.extra_files["/etc/bind/named.conf"] = name_config

        basic_bind_conf = textwrap.dedent(
            f"""\
            $TTL 1H
            @   IN  SOA {ns_name}.{zone_name}. admin.{zone_name}. (
                    2025111101 ; Serial
                    1H         ; Refresh
                    15M        ; Retry
                    1W         ; Expire
                    1D )       ; Minimum

                IN  NS  {ns_name}.{zone_name}.
            {ns_name} IN  A   {dns.ip_address}
            """
        )
        for web_idx, web in enumerate(pod_webservers[dns_idx]):
            basic_bind_conf += f"web{web_idx} IN  A  {web.ip_address}\n"

        dns.extra_files[f"/etc/bind/db.{zone_name}"] = basic_bind_conf
        dns.cmd_list.append("systemctl start named")

    # Webserver configs
    for web in tot_webservers:
        web.cmd_list.append("nohup python3 -m http.server 80 &")

    # ---- Write output ----
    out_path = os.path.abspath(output_dir)
    os.makedirs(out_path, exist_ok=True)

    all_machines: list[RouterMeta | HostMeta] = (
        tot_super_spines
        + tot_spines
        + tot_leaves
        + tot_dns
        + tot_webservers
        + tot_clients
    )

    lab_conf_lines = [
        'LAB_NAME="dc_clos"',
        'LAB_DESCRIPTION="DC Clos topology - generated by kathara_lab_generator"',
        "",
    ]

    for meta in all_machines:
        # lab.conf: interfaces (links)
        for eth_idx, collision_domain in meta.links:
            lab_conf_lines.append(f'{meta.name}[{eth_idx}]="{collision_domain}"')
        # lab.conf: options
        lab_conf_lines.append(f'{meta.name}[image]="{meta.image}"')
        lab_conf_lines.append(f"{meta.name}[cpus]={meta.cpus}")
        lab_conf_lines.append(f'{meta.name}[mem]="{meta.mem}"')
        lab_conf_lines.append("")

    with open(os.path.join(out_path, "lab.conf"), "w", encoding="utf-8") as f:
        f.write("\n".join(lab_conf_lines))

    for meta in all_machines:
        # Write startup file
        startup_path = os.path.join(out_path, f"{meta.name}.startup")
        with open(startup_path, "w", encoding="utf-8") as f:
            f.write("\n".join(meta.cmd_list))

        # Write extra config files into host subfolder
        if meta.extra_files:
            host_dir = os.path.join(out_path, meta.name)
            for file_path, content in meta.extra_files.items():
                # file_path is e.g. /etc/frr/frr.conf -> host_dir/etc/frr/frr.conf
                rel_path = file_path.lstrip("/")
                full_path = os.path.join(host_dir, rel_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(content)

    return out_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate Kathara lab config for DC Clos BGP"
    )
    parser.add_argument(
        "-s",
        "--size",
        choices=["s", "m", "l"],
        default="s",
        help="Topology size (default: s)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output directory (default: topology/ next to this script)",
    )
    args = parser.parse_args()

    out = generate_dc_clos_service_topology(
        topo_size=args.size,
        output_dir=args.output or os.path.join(SCRIPT_DIR, "topology"),
    )
    print(f"Lab configuration generated at: {out}")
    print("Contents:")
    for name in sorted(os.listdir(out)):
        p = os.path.join(out, name)
        if os.path.isfile(p):
            print(f"  {name}")
        else:
            print(f"  {name}/")
            for sub in sorted(os.listdir(p)):
                sp = os.path.join(p, sub)
                if os.path.isfile(sp):
                    print(f"    {sub}")
                else:
                    for ssub in sorted(os.listdir(sp)):
                        print(f"    {sub}/{ssub}")
