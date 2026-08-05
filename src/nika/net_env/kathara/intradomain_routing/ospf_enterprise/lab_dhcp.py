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


class RouterMeta:
    def __init__(self, name, machine: Machine, eth_index, cmd_list):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.router_id = ""  # the first interface IP
        self.frr_config = ""
        self.frr_ospf_configs = []
        self.host_network = None  # for dist nodes


class SwitchMeta:
    def __init__(self, name, machine: Machine, eth_index, cmd_list):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list


class HostMeta:
    def __init__(self, name, machine: Machine, eth_index, cmd_list):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.ip_address = None


class OSPFEnterpriseDHCP(NetworkEnvBase):
    LAB_NAME = "ospf_enterprise_dhcp"
    TOPO_LEVEL = "hard"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = [
        "arp",
        "link",
        "web",
        "icmp",
        "frr",
        "dns",
        "ospf",
        "dhcp",
        "pc",
        "mac",
        "http",
        "load_balancer",
    ]

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = "An enterprise OSPF network with multiple areas."

        match topo_size:
            case "s":
                DIST_SW_COUNT, ACCESS_SW_PER_DIST, HOST_PER_ACCESS = (
                    1,
                    1,
                    1,
                )  # per core router
            case "m":
                DIST_SW_COUNT, ACCESS_SW_PER_DIST, HOST_PER_ACCESS = 2, 2, 2
            case "l":
                DIST_SW_COUNT, ACCESS_SW_PER_DIST, HOST_PER_ACCESS = 4, 4, 4

        # core layer, only core1 and core2 connect dist routers, core3 connect to server subnet
        core_routers = {}
        for core_id in range(1, 4):
            router_core = self.lab.new_machine(
                f"router_core_{core_id}",
                **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"},
            )
            router_core_meta = RouterMeta(
                name=f"router_core_{core_id}",
                machine=router_core,
                eth_index=0,
                cmd_list=[],
            )
            core_routers[core_id] = router_core_meta

        # distribution layer
        core_dists = {}
        dist_accesses = {}
        access_hosts = {}
        for core_id in range(1, 3):  # core1 and core2
            core_dists[core_id] = []
            for dist_id in range(1, DIST_SW_COUNT + 1):
                dist_name = f"router_dist_{core_id}_{dist_id}"
                router_dist = self.lab.new_machine(
                    dist_name,
                    **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"},
                )
                dist_meta = RouterMeta(
                    name=dist_name,
                    machine=router_dist,
                    eth_index=0,
                    cmd_list=[],
                )
                core_dists[core_id].append(dist_meta)

                # access layer
                dist_key = f"{core_id}_{dist_id}"
                dist_accesses[dist_key] = []
                for access_id in range(1, ACCESS_SW_PER_DIST + 1):
                    access_name = f"switch_access_{core_id}_{dist_id}_{access_id}"
                    router_access = self.lab.new_machine(
                        access_name,
                        **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"},
                    )
                    access_meta = SwitchMeta(
                        name=access_name,
                        machine=router_access,
                        eth_index=0,
                        cmd_list=[],
                    )
                    dist_accesses[dist_key].append(access_meta)

                    # hosts
                    access_key = f"{core_id}_{dist_id}_{access_id}"
                    access_hosts[access_key] = []
                    for host_id in range(1, HOST_PER_ACCESS + 1):
                        host_name = f"pc_{core_id}_{dist_id}_{access_id}_{host_id}"
                        host_machine = self.lab.new_machine(
                            host_name,
                            **{
                                "image": "kathara/nika-base",
                                "cpus": 0.5,
                                "mem": "256m",
                            },
                        )
                        host_meta = HostMeta(
                            name=host_name,
                            machine=host_machine,
                            eth_index=0,
                            cmd_list=[],
                        )
                        access_hosts[access_key].append(host_meta)

        """Add servers"""
        servers = {}
        tot_dns = []
        web_server_count = 4  # total web servers
        web_servers = []
        server_network = IPv4Network("10.200.0.0/24")
        server_ip_gen = server_network.hosts()
        server_gateway_ip = next(server_ip_gen)

        # dns
        host_name = "dns_server"
        host_machine = self.lab.new_machine(
            host_name, **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"}
        )
        host_meta = HostMeta(
            name=host_name,
            machine=host_machine,
            eth_index=0,
            cmd_list=[],
        )
        servers[host_name] = host_meta
        tot_dns.append(host_meta)
        # web
        for web_idx in range(web_server_count):
            host_name = f"web_server_{web_idx}"
            host_machine = self.lab.new_machine(
                host_name, **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"}
            )
            host_meta = HostMeta(
                name=host_name,
                machine=host_machine,
                eth_index=0,
                cmd_list=[],
            )
            servers[host_name] = host_meta
            web_servers.append(host_meta)

        # load balancer and its backend servers
        lb_name = "load_balancer"
        lb_machine = self.lab.new_machine(
            lb_name, **{"image": "kathara/nika-nginx", "cpus": 0.5, "mem": "256m"}
        )
        lb_meta = HostMeta(
            name=lb_name,
            machine=lb_machine,
            eth_index=0,
            cmd_list=[],
        )
        servers[lb_name] = lb_meta
        lb_backends = []
        for web_idx in range(3):  # 3 backend servers
            backend_name = f"backend_web_{web_idx}"
            backend_machine = self.lab.new_machine(
                backend_name,
                **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"},
            )
            backend_meta = HostMeta(
                name=backend_name,
                machine=backend_machine,
                eth_index=0,
                cmd_list=[],
            )
            lb_backends.append(backend_meta)

        # dhcp
        host_name = "dhcp_server"
        host_machine = self.lab.new_machine(
            host_name, **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"}
        )
        host_meta = HostMeta(
            name=host_name,
            machine=host_machine,
            eth_index=0,
            cmd_list=[],
        )
        servers[host_name] = host_meta

        # server access switch
        server_router = self.lab.new_machine(
            "server_access_router",
            **{"image": "kathara/nika-frr", "cpus": 0.5, "mem": "256m"},
        )
        server_router_meta = RouterMeta(
            name="server_access_router",
            machine=server_router,
            eth_index=0,
            cmd_list=[],
        )

        # Now connect the layers and assign IPs and OSPF configs
        infra_pool = IPv4Network("172.16.0.0/16")
        # Create a generator of /31s
        subnets_infra = list(infra_pool.subnets(new_prefix=31))

        # add links between core routers (1-2, 1-3, 2-3)
        for core_id1 in range(1, 3):
            for core_id2 in range(core_id1 + 1, 4):
                core_meta1 = core_routers[core_id1]
                core_meta2 = core_routers[core_id2]
                self.lab.connect_machine_to_link(
                    core_meta1.machine.name,
                    f"{core_meta1.machine.name}_{core_meta2.machine.name}",
                )
                self.lab.connect_machine_to_link(
                    core_meta2.machine.name,
                    f"{core_meta1.machine.name}_{core_meta2.machine.name}",
                )
                subnet = subnets_infra.pop(0)
                a_ip, b_ip = assign_p2p_ips(subnet)
                core_meta1.cmd_list.append(
                    f"ip addr add {a_ip} dev eth{core_meta1.eth_index}"
                )
                core_meta2.cmd_list.append(
                    f"ip addr add {b_ip} dev eth{core_meta2.eth_index}"
                )

                # add OSPF area network
                core_meta1.frr_ospf_configs.append(f"network {subnet} area 0")
                core_meta2.frr_ospf_configs.append(f"network {subnet} area 0")

                # add router ID
                if core_meta1.router_id == "":
                    core_meta1.router_id = a_ip.split("/")[0]
                if core_meta2.router_id == "":
                    core_meta2.router_id = b_ip.split("/")[0]
                core_meta1.eth_index += 1
                core_meta2.eth_index += 1

        # add links between core and dist
        for core_id in range(1, 3):  # core1 and core2
            core_meta = core_routers[core_id]
            for dist_meta in core_dists[core_id]:
                self.lab.connect_machine_to_link(
                    core_meta.machine.name,
                    f"{core_meta.machine.name}_{dist_meta.machine.name}",
                )
                self.lab.connect_machine_to_link(
                    dist_meta.machine.name,
                    f"{core_meta.machine.name}_{dist_meta.machine.name}",
                )
                subnet = subnets_infra.pop(0)
                a_ip, b_ip = assign_p2p_ips(subnet)
                core_meta.cmd_list.append(
                    f"ip addr add {a_ip} dev eth{core_meta.eth_index}"
                )
                core_meta.eth_index += 1
                dist_meta.cmd_list.append(
                    f"ip addr add {b_ip} dev eth{dist_meta.eth_index}"
                )

                # add OSPF area network
                core_meta.frr_ospf_configs.append(f"network {subnet} area 1")
                dist_meta.frr_ospf_configs.append(f"network {subnet} area 1")

                # add router ID
                if core_meta.router_id == "":
                    core_meta.router_id = a_ip.split("/")[0]
                if dist_meta.router_id == "":
                    dist_meta.router_id = b_ip.split("/")[0]
                dist_meta.eth_index += 1

        # add links between dist and access
        for core_id in range(1, 3):
            for dist_id in range(1, DIST_SW_COUNT + 1):
                dist_key = f"{core_id}_{dist_id}"
                dist_meta = core_dists[core_id][dist_id - 1]
                dist_meta.cmd_list.append("brctl addbr br0")
                dist_meta.cmd_list.append("ip link set br0 up")
                for access_meta in dist_accesses[dist_key]:
                    self.lab.connect_machine_to_link(
                        dist_meta.machine.name,
                        f"{dist_meta.machine.name}_{access_meta.machine.name}",
                    )
                    self.lab.connect_machine_to_link(
                        access_meta.machine.name,
                        f"{dist_meta.machine.name}_{access_meta.machine.name}",
                    )
                    # add bridge setup command
                    dist_meta.cmd_list.append(
                        f"brctl addif br0 eth{dist_meta.eth_index}"
                    )
                    access_meta.cmd_list.append("brctl addbr br0")
                    access_meta.cmd_list.append("ip link set br0 up")
                    access_meta.cmd_list.append(
                        f"brctl addif br0 eth{access_meta.eth_index}"
                    )
                    dist_meta.eth_index += 1
                    access_meta.eth_index += 1

        # add links between access and hosts
        for core_id in range(1, 3):
            for dist_id in range(1, DIST_SW_COUNT + 1):
                dist_network = IPv4Network(f"10.{core_id}.{dist_id}.0/24")
                host_ip_gen = dist_network.hosts()
                dist_key = f"{core_id}_{dist_id}"

                # add network to dist router OSPF config
                dist_meta = core_dists[core_id][dist_id - 1]
                dist_meta.frr_ospf_configs.append(f"network {dist_network} area 1")

                # add IP assignment to br0
                default_gateway_ip = next(host_ip_gen)
                dist_meta.cmd_list.append(
                    f"ip addr add {default_gateway_ip}/{dist_network.prefixlen} dev br0"
                )

                for access_id in range(1, ACCESS_SW_PER_DIST + 1):
                    access_key = f"{core_id}_{dist_id}_{access_id}"
                    access_meta = dist_accesses[dist_key][access_id - 1]
                    access_meta.host_network = dist_network
                    for host_meta in access_hosts[access_key]:
                        self.lab.connect_machine_to_link(
                            access_meta.machine.name,
                            f"{access_meta.machine.name}_{host_meta.machine.name}",
                        )
                        self.lab.connect_machine_to_link(
                            host_meta.machine.name,
                            f"{access_meta.machine.name}_{host_meta.machine.name}",
                        )
                        host_meta.eth_index += 1

                        # attach to bridge
                        access_meta.cmd_list.append(
                            f"brctl addif br0 eth{access_meta.eth_index}"
                        )
                        access_meta.eth_index += 1

        server_router_meta.cmd_list.append("brctl addbr br0")
        server_router_meta.cmd_list.append("ip link set br0 up")
        # add ospf config
        server_router_meta.frr_ospf_configs.append(f"network {server_network} area 0")

        # add default gateway to server access switch
        server_router_meta.cmd_list.append(
            f"ip addr add {server_gateway_ip}/{server_network.prefixlen} dev br0"
        )

        # connect servers to access switch
        for server_name, server_meta in servers.items():
            self.lab.connect_machine_to_link(
                server_router_meta.machine.name,
                f"{server_router_meta.machine.name}_{server_meta.machine.name}",
            )
            self.lab.connect_machine_to_link(
                server_meta.machine.name,
                f"{server_router_meta.machine.name}_{server_meta.machine.name}",
            )
            # add bridge setup command
            server_router_meta.cmd_list.append(
                f"brctl addif br0 eth{server_router_meta.eth_index}"
            )
            # attach server side
            server_ip = next(server_ip_gen)
            server_meta.cmd_list.append(
                f"ip addr add {server_ip}/{server_network.prefixlen} dev eth{server_meta.eth_index}"
            )
            server_meta.ip_address = str(server_ip)
            # add default route
            server_meta.cmd_list.append(
                f"ip route add default via {server_gateway_ip} dev eth{server_meta.eth_index}"
            )
            server_meta.eth_index += 1
            server_router_meta.eth_index += 1

            # add dhcp relay
            if "dhcp" in server_name:
                for core_id in range(1, 3):
                    for dist_id in range(1, DIST_SW_COUNT + 1):
                        dist_meta = core_dists[core_id][dist_id - 1]
                        dist_meta.cmd_list.append(
                            f"dhcrelay  -i br0 -i eth0 {server_ip}"
                        )  # make sure eth0 is connected to core router

        # address assignment for load balancer backend servers
        lb_network = IPv4Network("20.200.0.0/24")
        lb_ip_gen = lb_network.hosts()

        self.lab.connect_machine_to_link(lb_meta.machine.name, "lb_backend_link")
        lb_gateway = next(lb_ip_gen)
        lb_meta.cmd_list.append(
            f"ip addr add {lb_gateway}/{lb_network.prefixlen} dev eth{lb_meta.eth_index}"
        )
        lb_meta.eth_index += 1

        # connect backend servers to load balancer
        for web_idx in range(3):  # 3 backend servers
            backend_meta = lb_backends[web_idx]
            # attach to load balancer
            self.lab.connect_machine_to_link(
                backend_meta.machine.name, "lb_backend_link"
            )
            # backend server side
            backend_meta.cmd_list.append(
                f"ip addr add {next(lb_ip_gen)}/{lb_network.prefixlen} dev eth{backend_meta.eth_index}"
            )
            backend_meta.cmd_list.append(
                f"ip route add default via {lb_gateway} dev eth{backend_meta.eth_index}"
            )
            backend_meta.eth_index += 1

        # connect server access switch to core3
        core3_meta = core_routers[3]
        self.lab.connect_machine_to_link(
            core3_meta.machine.name,
            f"{core3_meta.machine.name}_{server_router_meta.machine.name}",
        )
        self.lab.connect_machine_to_link(
            server_router_meta.machine.name,
            f"{core3_meta.machine.name}_{server_router_meta.machine.name}",
        )
        subnet = subnets_infra.pop(0)
        a_ip, b_ip = assign_p2p_ips(subnet)
        core3_meta.cmd_list.append(f"ip addr add {a_ip} dev eth{core3_meta.eth_index}")
        server_router_meta.cmd_list.append(
            f"ip addr add {b_ip} dev eth{server_router_meta.eth_index}"
        )
        # add OSPF area network
        core3_meta.frr_ospf_configs.append(f"network {subnet} area 0")
        server_router_meta.frr_ospf_configs.append(f"network {subnet} area 0")
        # add router ID
        if core3_meta.router_id == "":
            core3_meta.router_id = a_ip.split("/")[0]
        if server_router_meta.router_id == "":
            server_router_meta.router_id = b_ip.split("/")[0]
        core3_meta.eth_index += 1
        server_router_meta.eth_index += 1

        """
        configure FRR and startup scripts for all routers, switches, and hosts
        """
        # Add basic configuration to the core routers
        for core_id, core_meta in core_routers.items():
            # general conf for frr
            core_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/ospf/daemons")), "/etc/frr/daemons"
            )
            core_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/ospf/vtysh.conf")),
                "/etc/frr/vtysh.conf",
            )
            core_meta.frr_config = FRR_BASE_TEMPLATE.format(
                router_id=core_meta.router_id,
                ospf_networks="\n ".join(core_meta.frr_ospf_configs),
            )
            core_meta.machine.create_file_from_string(
                core_meta.frr_config, "/etc/frr/frr.conf"
            )

            # startup file
            core_meta.cmd_list.append("service frr start")
            self.lab.create_file_from_list(
                core_meta.cmd_list,
                f"{core_meta.machine.name}.startup",
            )

        # add basic configuration to the dist switches
        for core_id, dist_metas in core_dists.items():
            for dist_meta in dist_metas:
                # general conf for frr
                dist_meta.machine.create_file_from_path(
                    str(pkg_path("net_env/kathara/utils/ospf/daemons")),
                    "/etc/frr/daemons",
                )
                dist_meta.machine.create_file_from_path(
                    str(pkg_path("net_env/kathara/utils/ospf/vtysh.conf")),
                    "/etc/frr/vtysh.conf",
                )
                dist_meta.frr_config = FRR_BASE_TEMPLATE.format(
                    router_id=dist_meta.router_id,
                    ospf_networks="\n ".join(dist_meta.frr_ospf_configs),
                )
                dist_meta.machine.create_file_from_string(
                    dist_meta.frr_config, "/etc/frr/frr.conf"
                )

                # startup file
                dist_meta.cmd_list.append("service frr start")
                self.lab.create_file_from_list(
                    dist_meta.cmd_list,
                    f"{dist_meta.machine.name}.startup",
                )

        # add configurations for server switch
        server_router_meta.machine.create_file_from_path(
            str(pkg_path("net_env/kathara/utils/ospf/daemons")), "/etc/frr/daemons"
        )
        server_router_meta.machine.create_file_from_path(
            str(pkg_path("net_env/kathara/utils/ospf/vtysh.conf")),
            "/etc/frr/vtysh.conf",
        )
        server_router_meta.frr_config = FRR_BASE_TEMPLATE.format(
            router_id=server_router_meta.router_id,
            ospf_networks="\n ".join(server_router_meta.frr_ospf_configs),
        )
        server_router_meta.machine.create_file_from_string(
            server_router_meta.frr_config, "/etc/frr/frr.conf"
        )
        server_router_meta.cmd_list.append("service frr start")
        self.lab.create_file_from_list(
            server_router_meta.cmd_list,
            f"{server_router_meta.machine.name}.startup",
        )

        # add configurations for access switches
        for dist_key, access_metas in dist_accesses.items():
            for access_meta in access_metas:
                # startup file
                self.lab.create_file_from_list(
                    access_meta.cmd_list,
                    f"{access_meta.machine.name}.startup",
                )

        # add configurations for hosts
        for access_key, host_metas in access_hosts.items():
            for host_meta in host_metas:
                # startup file
                host_meta.cmd_list.append(
                    "printf 'timeout 1;\nretry 1;\n' >> /etc/dhcp/dhclient.conf"
                )
                host_meta.cmd_list.append("dhclient -d eth0")

                self.lab.create_file_from_list(
                    host_meta.cmd_list,
                    f"{host_meta.machine.name}.startup",
                )

        # add configurations for dns server
        dns_meta = servers["dns_server"]
        zone_name = "local"
        ns_name = "ns1"
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
        dns_meta.machine.create_file_from_string(name_config, "/etc/bind/named.conf")

        basic_bind_conf = textwrap.dedent(f"""\
                            $TTL 1H
                            @   IN  SOA {ns_name}.{zone_name}. admin.{zone_name}. (
                                    2025111101 ; Serial
                                    1H         ; Refresh
                                    15M        ; Retry
                                    1W         ; Expire
                                    1D )       ; Minimum

                                IN  NS  {ns_name}.{zone_name}.
                            {ns_name} IN  A   {dns_meta.ip_address}\

        """)
        for web_idx, web in enumerate(web_servers):
            basic_bind_conf += f"web{web_idx} IN  A  {web.ip_address}\n"
        basic_bind_conf += f"web99 IN  A  {lb_meta.ip_address}\n"
        dns_meta.machine.create_file_from_string(
            basic_bind_conf,
            f"/etc/bind/db.{zone_name}",
        )
        dns_meta.cmd_list.append("systemctl start named")
        self.lab.create_file_from_list(
            dns_meta.cmd_list,
            f"{dns_meta.machine.name}.startup",
        )

        # add configurations for web servers
        for web_idx, web_meta in enumerate(web_servers):
            web_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/web/web_server.py")),
                "web_server.py",
            )
            web_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/web/web_server.service")),
                "/etc/systemd/system/web_server.service",
            )
            web_meta.cmd_list.append("systemctl daemon-reload")
            web_meta.cmd_list.append("systemctl enable web_server")
            web_meta.cmd_list.append("systemctl start web_server")
            self.lab.create_file_from_list(
                web_meta.cmd_list,
                f"{web_meta.machine.name}.startup",
            )
        # add configurations for load balancer backend servers
        for web_idx, backend_meta in enumerate(lb_backends):
            backend_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/web/web_server.py")),
                "web_server.py",
            )
            backend_meta.machine.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/web/web_server.service")),
                "/etc/systemd/system/web_server.service",
            )
            backend_meta.cmd_list.append("systemctl daemon-reload")
            backend_meta.cmd_list.append("systemctl enable web_server")
            backend_meta.cmd_list.append("systemctl start web_server")
            self.lab.create_file_from_list(
                backend_meta.cmd_list,
                f"{backend_meta.machine.name}.startup",
            )

        # add configurations for dhcp server
        dhcp_meta = servers["dhcp_server"]
        dhcp_config = textwrap.dedent("""\
            default-lease-time 30;
            max-lease-time 60;
            authoritative;
        """)
        for core_id in range(1, 3):
            for dist_id in range(1, DIST_SW_COUNT + 1):
                dist_network = IPv4Network(f"10.{core_id}.{dist_id}.0/24")
                dhcp_config += textwrap.dedent(
                    f"""\
                    subnet {dist_network.network_address} netmask {dist_network.netmask} {{
                        range {dist_network.network_address + 10} {dist_network.network_address + 100};
                        option routers {dist_network.network_address + 1};
                        option domain-name-servers {" ,".join([dns.ip_address for dns in tot_dns])};
                    }}
                """
                )
        dhcp_config += textwrap.dedent("""\
            subnet 10.200.0.0 netmask 255.255.255.0 {
            # pass
            }
        """)
        dhcp_meta.machine.create_file_from_string(dhcp_config, "/etc/dhcp/dhcpd.conf")
        # attach to eth0
        dhcp_meta.machine.create_file_from_string(
            textwrap.dedent("""\
            INTERFACESv4="eth0"
            DHCPDv4_CONF=/etc/dhcp/dhcpd.conf
            DHCPDv4_PID=/var/run/dhcpd.pid
            """),
            "/etc/default/isc-dhcp-server",
        )
        # startup file
        dhcp_meta.cmd_list.append("systemctl start isc-dhcp-server")
        self.lab.create_file_from_list(
            dhcp_meta.cmd_list,
            f"{dhcp_meta.machine.name}.startup",
        )

        # add configurations for load balancer
        lb_meta.machine.create_file_from_path(
            os.path.join(cur_path, "nginx.conf"),
            "/etc/nginx/nginx.conf",
        )
        lb_meta.cmd_list.append("service nginx start")
        self.lab.create_file_from_list(
            lb_meta.cmd_list,
            "load_balancer.startup",
        )

        # load machines after initialization
        self.load_machines()
        self.desc = (
            "An enterprise hierarchical network using OSPF with multiple areas, built from three core routers, distribution routers, and bridged access switches."
            "User hosts sit in subnets of the form 10.<core>.<dist>.0/24, obtain their IP configuration via DHCP (with dist-layer DHCP relay to a central DHCP server), "
            "and reach a server farm in 10.200.0.0/24 that hosts a DNS server for the local zone, several Apache web servers (web0.local…web3.local), "
            "An Nginx HTTP load balancer published as web99.local, which load-balances requests to three backend web servers."
            "Note that by design the backend web servers should not be directly accessible from the hosts. "
            "All infrastructure and server networks are advertised by FRR OSPF so that hosts can resolve and access the web services end-to-end."
        )

        # add the website urls
        self.web_urls = []
        for web_idx, web in enumerate(web_servers):
            url = f"http://web{web_idx}.local"
            self.web_urls.append(url)
        lb_url = "http://web99.local"
        self.web_urls.append(lb_url)

        # add DNS
        self.dns_servers = [dns.ip_address for dns in tot_dns]

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.intradomain_routing.ospf_enterprise.verify import (
            verify_ospf_enterprise_lab,
        )

        return verify_ospf_enterprise_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            mode="dhcp",
        )
