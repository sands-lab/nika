import os
import textwrap
from ipaddress import IPv4Interface, IPv4Network
from typing import Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase

cur_path = os.path.dirname(os.path.abspath(__file__))


# P2P links use 172.16.0.0/16 with /31 per link.
# Host access networks use 10.<pod>.<leaf>.0/24 with leaf .1 and host .2
def assign_p2p_ips(subnet):
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


class RouterMeta:
    def __init__(self, name, machine: Machine, eth_index, cmd_list, AS_number):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.AS_number = AS_number
        self.router_id = ""  # the first interface IP
        self.frr_config = ""
        self.frr_neighbor_configs = []
        self.host_network = None  # for leaves


class HostMeta:
    def __init__(self, name, machine: Machine, eth_index, cmd_list):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.ip_address = None


class DCClosService(NetworkEnvBase):
    LAB_NAME = "dc_clos_service"
    TOPO_LEVEL = "hard"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = ["arp", "link", "mac", "bgp", "icmp", "frr", "dns", "pc", "http"]

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        if topo_size == "s":
            self.super_spine_count = 1
            self.spine_count = 2
            self.leaf_count = 2
        elif topo_size == "m":
            self.super_spine_count = 2
            self.spine_count = 4
            self.leaf_count = 4
        elif topo_size == "l":
            self.super_spine_count = 4
            self.spine_count = 4
            self.leaf_count = 8
        else:
            raise ValueError("Invalid topo_size. Choose from 's', 'm', 'l'.")

        pod_spines = {}
        pod_leaves = {}
        pod_dns = {}
        pod_webservers = {}

        tot_super_spines: list[RouterMeta] = []
        tot_spines: list[RouterMeta] = []
        tot_leaves: list[RouterMeta] = []
        tot_dns: list[HostMeta] = []
        tot_webservers: list[HostMeta] = []
        tot_clients: list[dict] = []

        infra_pool = IPv4Network("172.16.0.0/16")
        # Create a generator of /31s
        subnets31 = list(infra_pool.subnets(new_prefix=31))

        for ss in range(self.super_spine_count):
            ss_name = f"super_spine_router_{ss}"
            router_ss = self.lab.new_machine(
                ss_name, **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"}
            )
            router_ss_meta = RouterMeta(
                name=ss_name,
                machine=router_ss,
                eth_index=0,
                cmd_list=[],
                AS_number=65000,
            )
            tot_super_spines.append(router_ss_meta)

        for pod in range(self.super_spine_count):
            pod_spines[pod] = []
            for spine_id in range(self.spine_count):
                spine_name = f"spine_router_{pod}_{spine_id}"
                router_spine = self.lab.new_machine(
                    spine_name,
                    **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"},
                )
                spine_meta = RouterMeta(
                    name=spine_name,
                    machine=router_spine,
                    eth_index=0,
                    cmd_list=[],
                    AS_number=65100 + 10 * pod + spine_id,
                )
                pod_spines[pod].append(spine_meta)
                tot_spines.append(spine_meta)

            pod_leaves[pod] = []
            for leaf_id in range(self.leaf_count):
                leaf_name = f"leaf_router_{pod}_{leaf_id}"
                router_leaf = self.lab.new_machine(
                    leaf_name,
                    **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"},
                )
                leaf_meta = RouterMeta(
                    name=leaf_name,
                    machine=router_leaf,
                    eth_index=0,
                    cmd_list=[],
                    AS_number=65200 + 10 * pod + leaf_id,
                )
                pod_leaves[pod].append(leaf_meta)
                tot_leaves.append(leaf_meta)

            pod_dns[pod] = []
            # a dns and three webserver per pod
            dns_name = f"dns_pod{pod}"
            dns_machine = self.lab.new_machine(
                dns_name, **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"}
            )
            dns_meta = HostMeta(
                name=dns_name,
                machine=dns_machine,
                eth_index=0,
                cmd_list=[],
            )
            pod_dns[pod].append(dns_meta)
            tot_dns.append(dns_meta)

            pod_webservers[pod] = []
            for host in range(self.leaf_count - 1):
                web_name = f"webserver{host}_pod{pod}"
                web_machine = self.lab.new_machine(
                    web_name,
                    **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"},
                )
                web_meta = HostMeta(
                    name=web_name,
                    machine=web_machine,
                    eth_index=0,
                    cmd_list=[],
                )
                pod_webservers[pod].append(web_meta)
                tot_webservers.append(web_meta)

        # add two client hosts outside the DC
        for client_id in range(self.super_spine_count):
            client_name = f"client_{client_id}"
            client_machine = self.lab.new_machine(
                client_name,
                **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"},
            )
            client_meta = HostMeta(
                name=client_name,
                machine=client_machine,
                eth_index=0,
                cmd_list=[],
            )
            tot_clients.append(client_meta)

        # add links between super spines and spines
        for pod in range(self.super_spine_count):
            super_spine_meta = tot_super_spines[pod]
            for spine_meta in tot_spines:
                self.lab.connect_machine_to_link(
                    super_spine_meta.machine.name,
                    f"{super_spine_meta.machine.name}_{spine_meta.machine.name}",
                )
                self.lab.connect_machine_to_link(
                    spine_meta.machine.name,
                    f"{super_spine_meta.machine.name}_{spine_meta.machine.name}",
                )
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

                # add BGP neighbor config
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
                # add router ID
                if super_spine_meta.router_id == "":
                    super_spine_meta.router_id = a_ip.split("/")[0]
                if spine_meta.router_id == "":
                    spine_meta.router_id = b_ip.split("/")[0]

        # add links between spines and leaves
        for pod in range(self.super_spine_count):
            for spine_meta in pod_spines[pod]:
                for leaf_meta in pod_leaves[pod]:
                    self.lab.connect_machine_to_link(
                        spine_meta.machine.name,
                        f"{spine_meta.machine.name}_{leaf_meta.machine.name}",
                    )
                    self.lab.connect_machine_to_link(
                        leaf_meta.machine.name,
                        f"{spine_meta.machine.name}_{leaf_meta.machine.name}",
                    )
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
                    # add BGP neighbor config
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

                    # add router ID
                    if spine_meta.router_id == "":
                        spine_meta.router_id = a_ip.split("/")[0]
                    if leaf_meta.router_id == "":
                        leaf_meta.router_id = b_ip.split("/")[0]

        # add links between leaves and internal hosts
        for pod in range(self.super_spine_count):
            pod_services = pod_dns[pod] + pod_webservers[pod]

            for idx in range(self.leaf_count):
                leaf_meta = pod_leaves[pod][idx]
                host = pod_services[idx]
                self.lab.connect_machine_to_link(
                    leaf_meta.machine.name,
                    f"{leaf_meta.machine.name}_{host.machine.name}",
                )
                self.lab.connect_machine_to_link(
                    host.machine.name, f"{leaf_meta.machine.name}_{host.machine.name}"
                )
                subnet = IPv4Network(f"10.{pod}.{idx}.0/24")
                leaf_ip = IPv4Interface(
                    f"{subnet.network_address + 1}/{subnet.prefixlen}"
                )
                host_ip = IPv4Interface(
                    f"{subnet.network_address + 2}/{subnet.prefixlen}"
                )
                leaf_meta.cmd_list.append(
                    f"ip addr add {leaf_ip} dev eth{leaf_meta.eth_index}"
                )
                leaf_meta.eth_index += 1
                host.cmd_list.append(f"ip addr add {host_ip} dev eth{host.eth_index}")
                host.cmd_list.append(
                    f"ip route add default via {leaf_ip.ip} dev eth{host.eth_index}"
                )
                host.eth_index += 1
                # add host network to leaf for redistribution
                leaf_meta.host_network = str(subnet)
                host.ip_address = str(host_ip.ip)

        # add link between client host and super spines
        for pod in range(self.super_spine_count):
            client_meta = tot_clients[pod]
            super_spine_meta = tot_super_spines[pod]
            self.lab.connect_machine_to_link(
                super_spine_meta.machine.name,
                f"{super_spine_meta.machine.name}_{client_meta.machine.name}",
            )
            self.lab.connect_machine_to_link(
                client_meta.machine.name,
                f"{super_spine_meta.machine.name}_{client_meta.machine.name}",
            )
            subnet = IPv4Network(f"192.168.{pod}.0/24")
            ss_ip = IPv4Interface(f"{subnet.network_address + 1}/{subnet.prefixlen}")
            client_ip = IPv4Interface(
                f"{subnet.network_address + 2}/{subnet.prefixlen}"
            )
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
            # add host network to super spine for redistribution
            super_spine_meta.host_network = str(subnet)
            client_meta.ip_address = str(client_ip.ip)

        # Add basic configuration to the spines
        for router_meta in tot_spines:
            # general conf for frr
            router_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/bgp/daemons")), "/etc/frr/daemons"
            )
            router_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/bgp/vtysh.conf")),
                "/etc/frr/vtysh.conf",
            )
            router_meta.frr_config = FRR_BASE_TEMPLATE.format(
                hostname=router_meta.name,
                AS_number=router_meta.AS_number,
                router_id=router_meta.router_id,
                network="",
                neighbor_add_configs=" ".join(router_meta.frr_neighbor_configs),
            )
            router_meta.machine.create_file_from_string(
                router_meta.frr_config, "/etc/frr/frr.conf"
            )

            # startup file
            router_meta.cmd_list.append("service frr start")
            self.lab.create_file_from_list(
                router_meta.cmd_list,
                f"{router_meta.machine.name}.startup",
            )

        # Add basic configuration to the leaves and super spines
        for router_meta in tot_leaves + tot_super_spines:
            # general conf for frr
            router_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/bgp/daemons")), "/etc/frr/daemons"
            )
            router_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/bgp/vtysh.conf")),
                "/etc/frr/vtysh.conf",
            )
            router_meta.frr_config = FRR_BASE_TEMPLATE.format(
                hostname=router_meta.name,
                AS_number=router_meta.AS_number,
                router_id=router_meta.router_id,
                network=f"network {router_meta.host_network}\n",
                neighbor_add_configs=" ".join(router_meta.frr_neighbor_configs),
            )
            router_meta.machine.create_file_from_string(
                router_meta.frr_config, "/etc/frr/frr.conf"
            )

            # startup file
            router_meta.cmd_list.append("service frr start")
            self.lab.create_file_from_list(
                router_meta.cmd_list,
                f"{router_meta.machine.name}.startup",
            )

        # client configuration
        for host in tot_clients:
            ns_add_cmd = ""
            for dns in tot_dns:
                ns_add_cmd += f"nameserver {dns.ip_address}\n"
            host.machine.create_file_from_string(ns_add_cmd, "/etc/resolv.conf")

            self.lab.create_file_from_list(
                host.cmd_list,
                f"{host.machine.name}.startup",
            )
        # dns configuration
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
            dns.machine.create_file_from_string(name_config, "/etc/bind/named.conf")

            basic_bind_conf = textwrap.dedent(f"""\
                                $TTL 1H
                                @   IN  SOA {ns_name}.{zone_name}. admin.{zone_name}. (
                                        2025111101 ; Serial
                                        1H         ; Refresh
                                        15M        ; Retry
                                        1W         ; Expire
                                        1D )       ; Minimum

                                    IN  NS  {ns_name}.{zone_name}.
                                {ns_name} IN  A   {dns.ip_address}\

            """)
            for web_idx, web in enumerate(pod_webservers[dns_idx]):
                basic_bind_conf += f"web{web_idx} IN  A  {web.ip_address}\n"

            dns.machine.create_file_from_string(
                basic_bind_conf,
                f"/etc/bind/db.{zone_name}",
            )
            dns.cmd_list.append("systemctl start named")
            self.lab.create_file_from_list(
                dns.cmd_list,
                f"{dns.machine.name}.startup",
            )

        # webserver configuration
        for web in tot_webservers:
            web.cmd_list.append("nohup python3 -m http.server 80 &")
            self.lab.create_file_from_list(
                web.cmd_list,
                f"{web.machine.name}.startup",
            )

        # load machines after initialization
        self.load_machines()
        self.desc = (
            "A 4-tier data center Clos (super-spine, spine, leaf, service hosts) using EBGP: all inter-router links are /31s from 172.16.0.0/16, "
            "and each leaf connects to one service subnet 10.<pod>.<idx>.0/24 that it advertises via BGP."
            "Each pod runs one DNS server (Bind, authoritative for zone podX) and several HTTP web servers; "
            "DNS provides A records like web0.pod0 pointing to the web servers, which run python3 -m http.server on port 80."
            "External client hosts sit in 192.168.<pod>.0/24 networks, use the pod DNS servers in /etc/resolv.conf, "
            "and access the services via URLs such as http://web0.pod0 over the routed Clos fabric."
        )

        self.web_urls = []
        for pod_idx in range(self.super_spine_count):
            for web_idx in range(self.leaf_count - 1):
                self.web_urls.append(f"http://web{web_idx}.pod{pod_idx}")

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.data_center_routing.dc_clos_bgp.verify import (
            verify_dc_clos_service_lab,
        )

        return verify_dc_clos_service_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )
