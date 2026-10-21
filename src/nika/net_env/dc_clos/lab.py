"""DC Clos eBGP fabric with DNS and HTTP services on its leaf networks."""

from __future__ import annotations

import textwrap
from ipaddress import IPv4Interface, IPv4Network
from typing import Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.runtime.spec import NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

# P2P links use 172.16.0.0/16 with /31 per link.
# Leaf access networks use 10.<pod>.<leaf>.0/24 with leaf .1 and endpoint .2


def assign_p2p_ips(subnet):
    base = subnet.network_address
    ip0 = IPv4Interface(f"{base}/31")
    ip1 = IPv4Interface(f"{base + 1}/31")
    return str(ip0), str(ip1)


def fabric_dimensions(topo_size: Literal["s", "m", "l"]) -> tuple[int, int, int]:
    """Return (super_spine_count, spine_per_pod, leaf_per_pod)."""
    if topo_size == "s":
        return 1, 2, 2
    if topo_size == "m":
        return 2, 4, 4
    if topo_size == "l":
        return 4, 8, 8
    raise ValueError("topo_size should be s, m, or l.")


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

# glibc reads at most three ``nameserver`` lines from /etc/resolv.conf.
_RESOLV_MAXNS = 3

FRR_NEIGHBOR_ADD_TEMPLATE = """neighbor {neighbor_ip} remote-as {neighbor_as}
"""

DEVICE_PROFILES = ("frr", "routeros")
NATIVE_TOPO_SIZES = ("s",)


class RouterMeta:
    def __init__(self, name, machine: Machine, eth_index, cmd_list, AS_number):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.AS_number = AS_number
        self.router_id = ""
        self.frr_config = ""
        self.frr_neighbor_configs = []
        self.neighbor_ips: list[str] = []
        self.peers: list[tuple[str, int]] = []
        self.addresses: list[tuple[str, str]] = []
        self.host_network = None

    def add_address(self, cidr: str) -> None:
        """Assign ``cidr`` to the next data interface."""
        iface = f"eth{self.eth_index}"
        self.cmd_list.append(f"ip addr add {cidr} dev {iface}")
        self.addresses.append((iface, cidr))
        self.eth_index += 1

    def add_peer(self, address: str, asn: int) -> None:
        self.frr_neighbor_configs.append(
            FRR_NEIGHBOR_ADD_TEMPLATE.format(neighbor_ip=address, neighbor_as=asn)
        )
        self.neighbor_ips.append(address)
        self.peers.append((address, asn))


class HostMeta:
    def __init__(self, name, machine: Machine, eth_index, cmd_list):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.ip_address = None


class DCClos(NetworkEnvBase):
    ROUTING_PARAMETER_KINDS = frozenset({"bgp_exports"})
    LAB_NAME = "dc_clos"
    TOPO_LEVEL = "medium"

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        routing_parameters: dict | None = None,
        device_profile: Literal["frr", "routeros"] = "frr",
        **kwargs,
    ):
        super().__init__(**kwargs)
        if device_profile not in DEVICE_PROFILES:
            raise ValueError(
                f"dc_clos supports device profiles {', '.join(DEVICE_PROFILES)}; "
                f"got {device_profile!r}."
            )
        if device_profile != "frr" and topo_size not in NATIVE_TOPO_SIZES:
            raise ValueError(
                f"dc_clos with device_profile={device_profile!r} supports topo_size "
                f"{', '.join(NATIVE_TOPO_SIZES)} only: each RouterOS router boots "
                "a QEMU VM, so larger fabrics exceed the supported host budget."
            )
        if device_profile != "frr" and routing_parameters is not None:
            raise ValueError(
                "Solved routing parameters are supported only for FRR dc_clos routers."
            )
        self.device_profile = device_profile
        self.topo_size = topo_size
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()

        super_spine_count, spine_per_pod, leaf_per_pod = fabric_dimensions(topo_size)
        self.super_spine_count = super_spine_count
        self.spine_count = spine_per_pod
        self.leaf_count = leaf_per_pod

        pod_spines: dict[int, list[RouterMeta]] = {}
        pod_leaves: dict[int, list[RouterMeta]] = {}

        tot_super_spines: list[RouterMeta] = []
        tot_spines: list[RouterMeta] = []
        tot_leaves: list[RouterMeta] = []

        infra_pool = IPv4Network("172.16.0.0/16")
        subnets31 = list(infra_pool.subnets(new_prefix=31))

        spine_total = super_spine_count * spine_per_pod
        for ss in range(super_spine_count):
            tot_super_spines.append(
                self._new_router(
                    f"super_spine_router_{ss}",
                    65000 + ss,
                    data_interfaces=spine_total + 1,
                )
            )

        for pod in range(super_spine_count):
            pod_spines[pod] = []
            for spine_id in range(spine_per_pod):
                spine_meta = self._new_router(
                    f"spine_router_{pod}_{spine_id}",
                    65100 + 10 * pod + spine_id,
                    data_interfaces=super_spine_count + leaf_per_pod,
                )
                pod_spines[pod].append(spine_meta)
                tot_spines.append(spine_meta)

            pod_leaves[pod] = []
            for leaf_id in range(leaf_per_pod):
                leaf_meta = self._new_router(
                    f"leaf_router_{pod}_{leaf_id}",
                    65200 + 10 * pod + leaf_id,
                    data_interfaces=spine_per_pod + 1,
                )
                pod_leaves[pod].append(leaf_meta)
                tot_leaves.append(leaf_meta)

        # Super-spine ↔ all spines
        for pod in range(super_spine_count):
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
                super_spine_meta.add_address(a_ip)
                spine_meta.add_address(b_ip)
                super_spine_meta.add_peer(b_ip.split("/")[0], spine_meta.AS_number)
                spine_meta.add_peer(a_ip.split("/")[0], super_spine_meta.AS_number)
                if super_spine_meta.router_id == "":
                    super_spine_meta.router_id = a_ip.split("/")[0]
                if spine_meta.router_id == "":
                    spine_meta.router_id = b_ip.split("/")[0]

        # Spine ↔ leaf within each pod
        for pod in range(super_spine_count):
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
                    spine_meta.add_address(a_ip)
                    leaf_meta.add_address(b_ip)
                    spine_meta.add_peer(b_ip.split("/")[0], leaf_meta.AS_number)
                    leaf_meta.add_peer(a_ip.split("/")[0], spine_meta.AS_number)
                    if spine_meta.router_id == "":
                        spine_meta.router_id = a_ip.split("/")[0]
                    if leaf_meta.router_id == "":
                        leaf_meta.router_id = b_ip.split("/")[0]

        self._attach_service_workload(
            super_spine_count=super_spine_count,
            leaf_per_pod=leaf_per_pod,
            pod_leaves=pod_leaves,
            tot_super_spines=tot_super_spines,
        )
        # Every configured eBGP session, by router, for lab verification.
        self.bgp_neighbors = {
            meta.name: tuple(meta.neighbor_ips)
            for meta in tot_super_spines + tot_spines + tot_leaves
        }
        advertise_routers = tot_leaves + tot_super_spines
        plain_routers = tot_spines

        for router_meta in plain_routers:
            self._write_router(router_meta, networks=())

        for router_meta in advertise_routers:
            self._write_router(
                router_meta,
                networks=(router_meta.host_network,)
                if router_meta.host_network
                else (),
            )

        from nika.net_env.synthesis.parameters import apply_routing_parameters

        apply_routing_parameters(self, routing_parameters)

        self.load_machines()
        self.desc = (
            "A multi-tier data center Clos using EBGP with DNS/HTTP services. "
            "Each pod runs one BIND DNS server and HTTP servers on leaf subnets; "
            "external clients in 192.168.<pod>.0/24 resolve names such as "
            "web0.pod0 over the fabric."
        )
        self.web_urls = [
            f"http://web{web_idx}.pod{pod_idx}"
            for pod_idx in range(super_spine_count)
            for web_idx in range(leaf_per_pod - 1)
        ]

    def _new_router(self, name: str, asn: int, *, data_interfaces: int) -> RouterMeta:
        if self.device_profile == "routeros":
            from nika.net_env.utils.routeros.common import add_routeros_router

            machine = add_routeros_router(
                self.lab, name, data_interfaces=data_interfaces
            )
            # Container eth0 is the VM's management port.
            eth_index = 1
        else:
            machine = self.lab.new_machine(
                name, **{"image": nika_image("frr"), "cpus": 0.5, "mem": "256m"}
            )
            eth_index = 0
        self.declare_machine(
            name,
            role=NodeRole.ROUTER,
            capabilities=("linux", self.device_profile, "bgp"),
        )
        return RouterMeta(
            name=name,
            machine=machine,
            eth_index=eth_index,
            cmd_list=[],
            AS_number=asn,
        )

    def _write_router(self, router_meta: RouterMeta, *, networks) -> None:
        if self.device_profile == "routeros":
            from nika.net_env.utils.routeros.common import set_routeros_config
            from nika.net_env.utils.routeros.render import (
                render_base,
                render_ebgp,
                render_script,
            )

            set_routeros_config(
                router_meta.machine,
                render_script(
                    render_base(router_meta.name, router_meta.addresses),
                    render_ebgp(
                        router_meta.AS_number,
                        router_meta.router_id,
                        router_meta.peers,
                        networks=networks,
                    ),
                ),
            )
            return
        network = "".join(f"network {item}\n" for item in networks)
        self._write_frr_router(router_meta, network=network)

    def _write_frr_router(self, router_meta: RouterMeta, *, network: str) -> None:
        router_meta.machine.create_file_from_path(
            str(pkg_path("net_env/utils/kathara/bgp/daemons")), "/etc/frr/daemons"
        )
        router_meta.machine.create_file_from_path(
            str(pkg_path("net_env/utils/kathara/bgp/vtysh.conf")),
            "/etc/frr/vtysh.conf",
        )
        router_meta.frr_config = FRR_BASE_TEMPLATE.format(
            hostname=router_meta.name,
            AS_number=router_meta.AS_number,
            router_id=router_meta.router_id,
            network=network,
            neighbor_add_configs=" ".join(router_meta.frr_neighbor_configs),
        )
        router_meta.machine.create_file_from_string(
            router_meta.frr_config, "/etc/frr/frr.conf"
        )
        router_meta.cmd_list.append("service frr start")
        self.lab.create_file_from_list(
            router_meta.cmd_list,
            f"{router_meta.machine.name}.startup",
        )

    def _attach_service_workload(
        self,
        *,
        super_spine_count: int,
        leaf_per_pod: int,
        pod_leaves: dict[int, list[RouterMeta]],
        tot_super_spines: list[RouterMeta],
    ) -> None:
        pod_dns: dict[int, list[HostMeta]] = {}
        pod_webservers: dict[int, list[HostMeta]] = {}
        tot_dns: list[HostMeta] = []
        tot_webservers: list[HostMeta] = []
        tot_clients: list[HostMeta] = []

        for pod in range(super_spine_count):
            dns_name = f"dns_pod{pod}"
            dns_machine = self.lab.new_machine(
                dns_name, **{"image": nika_image("base"), "cpus": 0.5, "mem": "256m"}
            )
            self.declare_machine(
                dns_name,
                role=NodeRole.SERVICE,
                capabilities=("linux", "dns"),
                service_type="dns",
                reachability_target=True,
            )
            dns_meta = HostMeta(
                name=dns_name,
                machine=dns_machine,
                eth_index=0,
                cmd_list=[],
            )
            pod_dns[pod] = [dns_meta]
            tot_dns.append(dns_meta)

            pod_webservers[pod] = []
            for host in range(leaf_per_pod - 1):
                web_name = f"webserver{host}_pod{pod}"
                web_machine = self.lab.new_machine(
                    web_name,
                    **{"image": nika_image("base"), "cpus": 0.5, "mem": "256m"},
                )
                self.declare_machine(
                    web_name,
                    role=NodeRole.SERVICE,
                    capabilities=("linux", "http"),
                    service_type="web",
                    reachability_target=True,
                )
                web_meta = HostMeta(
                    name=web_name,
                    machine=web_machine,
                    eth_index=0,
                    cmd_list=[],
                )
                pod_webservers[pod].append(web_meta)
                tot_webservers.append(web_meta)

        for client_id in range(super_spine_count):
            client_name = f"client_{client_id}"
            client_machine = self.lab.new_machine(
                client_name,
                **{"image": nika_image("base"), "cpus": 0.5, "mem": "256m"},
            )
            self.declare_machine(
                client_name,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )
            tot_clients.append(
                HostMeta(
                    name=client_name,
                    machine=client_machine,
                    eth_index=0,
                    cmd_list=[],
                )
            )

        for pod in range(super_spine_count):
            pod_services = pod_dns[pod] + pod_webservers[pod]
            for idx in range(leaf_per_pod):
                leaf_meta = pod_leaves[pod][idx]
                host = pod_services[idx]
                self.lab.connect_machine_to_link(
                    leaf_meta.machine.name,
                    f"{leaf_meta.machine.name}_{host.machine.name}",
                )
                self.lab.connect_machine_to_link(
                    host.machine.name,
                    f"{leaf_meta.machine.name}_{host.machine.name}",
                )
                subnet = IPv4Network(f"10.{pod}.{idx}.0/24")
                leaf_ip = IPv4Interface(
                    f"{subnet.network_address + 1}/{subnet.prefixlen}"
                )
                host_ip = IPv4Interface(
                    f"{subnet.network_address + 2}/{subnet.prefixlen}"
                )
                leaf_meta.add_address(str(leaf_ip))
                host.cmd_list.append(f"ip addr add {host_ip} dev eth{host.eth_index}")
                host.cmd_list.append(
                    f"ip route add default via {leaf_ip.ip} dev eth{host.eth_index}"
                )
                host.eth_index += 1
                leaf_meta.host_network = str(subnet)
                host.ip_address = str(host_ip.ip)

        for pod in range(super_spine_count):
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
            super_spine_meta.add_address(str(ss_ip))
            client_meta.cmd_list.append(
                f"ip addr add {client_ip} dev eth{client_meta.eth_index}"
            )
            client_meta.cmd_list.append(
                f"ip route add default via {ss_ip.ip} dev eth{client_meta.eth_index}"
            )
            client_meta.eth_index += 1
            super_spine_meta.host_network = str(subnet)
            client_meta.ip_address = str(client_ip.ip)

        self.client_addresses = {
            client.name: client.ip_address for client in tot_clients
        }
        self.dns_servers = tuple(
            (dns.ip_address, f"ns{idx}.pod{idx}") for idx, dns in enumerate(tot_dns)
        )
        for host in tot_clients:
            ns_add_cmd = ""
            for dns in tot_dns:
                ns_add_cmd += f"nameserver {dns.ip_address}\n"
            host.machine.create_file_from_string(ns_add_cmd, "/etc/resolv.conf")
            self.lab.create_file_from_list(
                host.cmd_list,
                f"{host.machine.name}.startup",
            )

        # The client resolver reads at most _RESOLV_MAXNS nameservers, so with
        # more pods each server forwards the other pods' zones to their owner.
        forward = len(tot_dns) > _RESOLV_MAXNS
        recursion = (
            "recursion yes; dnssec-validation no; max-cache-ttl 1; max-ncache-ttl 1;"
            if forward
            else "recursion no;"
        )
        for dns_idx, dns in enumerate(tot_dns):
            zone_name = f"pod{dns_idx}"
            ns_name = f"ns{dns_idx}"
            name_config = textwrap.dedent(
                f"""\
                options {{
                    directory "/var/cache/bind";
                    listen-on port 53 {{ any; }};
                    allow-query     {{ any; }};
                    {recursion}
                }};

                zone "{zone_name}" IN {{
                    type master;
                    file "/etc/bind/db.{zone_name}";
                }};"""
            )
            if forward:
                name_config += "".join(
                    f'\n\nzone "pod{idx}" IN {{\n'
                    "    type forward;\n    forward only;\n"
                    f"    forwarders {{ {other.ip_address}; }};\n}};"
                    for idx, other in enumerate(tot_dns)
                    if idx != dns_idx
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

        for web in tot_webservers:
            web.cmd_list.extend(
                [
                    "mkdir -p /var/www",
                    "dd if=/dev/zero of=/var/www/small.bin bs=1024 count=16 "
                    "status=none 2>/dev/null || true",
                    "dd if=/dev/zero of=/var/www/large.bin bs=1M count=32 "
                    "status=none 2>/dev/null || true",
                    "cd /var/www && nohup python3 -m http.server 80 >/dev/null 2>&1 &",
                ]
            )
            self.lab.create_file_from_list(
                web.cmd_list,
                f"{web.machine.name}.startup",
            )

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        return ProbePath(
            src_host="client_0",
            dst_ip="10.0.1.2",
            http_url="http://web0.pod0/",
            http_name_url="http://web0.pod0/",
            control_plane_host="leaf_router_0_0",
            peer_host="webserver0_pod0",
        )

    def _native_verify(self, verify, **kwargs) -> dict:
        if self.device_profile == "frr":
            return verify(**kwargs)
        from nika.net_env.utils.routeros.common import (
            routeros_bgp_peer_query,
            with_routeros_config_check,
        )

        result = verify(bgp_peers=routeros_bgp_peer_query(), **kwargs)
        return with_routeros_config_check(
            kwargs["runtime"], sorted(self.bgp_neighbors), result
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.dc_clos.verify import verify_dc_clos_lab_startup

        return self._native_verify(
            verify_dc_clos_lab_startup,
            runtime=self._build_runtime(),
            scenario_name=self.LAB_NAME,
            nodes=tuple(self.lab.machines),
            bgp_neighbors=self.bgp_neighbors,
            client_addresses=self.client_addresses,
        )

    def verify_lab(self) -> dict:
        from nika.net_env.dc_clos.verify import verify_dc_clos_lab

        return self._native_verify(
            verify_dc_clos_lab,
            runtime=self._build_runtime(),
            scenario_name=self.LAB_NAME,
            nodes=tuple(self.lab.machines),
            bgp_neighbors=self.bgp_neighbors,
            client_addresses=self.client_addresses,
            dns_servers=self.dns_servers,
            web_urls=tuple(self.web_urls),
        )

    def deploy(self):
        if self.device_profile == "routeros":
            from nika.net_env.utils.routeros.common import require_routeros_image

            require_routeros_image()
        super().deploy()
