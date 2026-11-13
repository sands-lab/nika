"""DC Clos eBGP fabric with DNS and HTTP services on its leaf networks."""

from __future__ import annotations

from typing import Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.net_env.clos.requirements import communication_contract
from nika.net_env.dc_clos.design import router_asns, routing_links
from nika.net_env.utils.download_object import download_object_commands
from nika.config import pkg_path
from nika.net_env.utils.static_http import static_http_commands
from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.net_env.native_routers import NativeRouterEnv
from nika.net_env.synthesis.design import RoutingDesign
from nika.runtime.spec import NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

from nika.net_env.clos.design import (
    build_clos_fabric_model,
    fabric_dimensions,
    gateway_ip,
    rack_prefix,
)

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
    def __init__(self, name, machine: Machine, eth_index, cmd_list, AS_number, design):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.AS_number = AS_number
        self.router_id = ""
        self.frr_config = ""
        self.frr_neighbor_configs = []
        self.neighbor_ips: list[str] = []
        self.host_network = None
        self.design = design.router(name)
        self.design.asn = AS_number
        self.design.bgp_multipath = 32
        self.design.ebgp_requires_policy = False
        self.design.debug = ["bgp keepalives", "bgp updates in", "bgp updates out"]


class DCClos(NativeRouterEnv, NetworkEnvBase):
    ARCHIVE_JOBS = True
    ROUTING_PARAMETER_KINDS = frozenset({"bgp_exports"})
    L2_CAPABILITIES = frozenset({"arp-broadcast", "mac-learning"})
    STORES_RENDERED_CONFIGS = True
    LAB_NAME = "dc_clos"
    TOPO_LEVEL = "medium"
    NATIVE_PROFILE_ROLES = {
        "iosxr": frozenset({"spine"}),
        "routeros": frozenset({"spine", "leaf"}),
    }
    NATIVE_PROFILE_SIZES = {"iosxr": ("s",), "routeros": ("s",)}

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        routing_parameters: dict | None = None,
        device_profile: str | None = None,
        rendered: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.topo_size = topo_size
        self.model = build_clos_fabric_model(topo_size)
        self.validation_contract = communication_contract(
            self.model, scenario=self.LAB_NAME, topo_size=topo_size
        )
        self.spine_count, self.leaf_count, self.endpoints_per_leaf = fabric_dimensions(
            topo_size
        )
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.routing_design = RoutingDesign()
        routers = {}
        for name, asn in router_asns(self.model).items():
            machine = self.lab.new_machine(
                name, image=nika_image("frr"), cpus=0.5, mem="256m"
            )
            self.declare_machine(
                name, role=NodeRole.ROUTER, capabilities=("linux", "frr", "bgp")
            )
            routers[name] = RouterMeta(name, machine, 0, [], asn, self.routing_design)

        self.l2_endpoint_groups = tuple(
            tuple(e.name for e in self.model.endpoints_on_leaf(i))
            for i in range(1, self.model.leaf_count + 1)
        )
        self.client_addresses = {e.name: e.ip for e in self.model.client_endpoints()}
        dns = self.model.web_endpoints()[0]
        self.dns_servers = ((dns.ip, f"{dns.name.replace('_', '-')}.fabric"),)
        self.web_urls = list(self.model.web_urls)
        self.dns_zones = {dns.name: ("fabric",)}
        self.dns_records = {
            f"{e.name.replace('_', '-')}.fabric": e.name
            for e in self.model.web_endpoints()
        }
        dns_addresses = [dns.ip]
        for leaf_id, leaf in enumerate(self.model.leaves, start=1):
            meta = routers[leaf]
            meta.host_network = rack_prefix(leaf_id)
            meta.cmd_list.append("brctl addbr br0")
            for endpoint in self.model.endpoints_on_leaf(leaf_id):
                machine = self.lab.new_machine(
                    endpoint.name, image=nika_image("base"), cpus=0.5, mem="256m"
                )
                self.declare_machine(
                    endpoint.name,
                    role=NodeRole.SERVICE if endpoint.role == "web" else NodeRole.HOST,
                    capabilities=("linux", "http", "dns")
                    if endpoint.name == dns.name
                    else ("linux", "http")
                    if endpoint.role == "web"
                    else ("linux",),
                    service_type="web" if endpoint.role == "web" else None,
                    reachability_target=True,
                )
                link = f"{endpoint.name}_{leaf}"
                self.lab.connect_machine_to_link(endpoint.name, link)
                self.lab.connect_machine_to_link(leaf, link)
                meta.cmd_list.append(f"brctl addif br0 eth{meta.eth_index}")
                meta.eth_index += 1
                commands = [
                    f"ip link set eth0 address {endpoint.mac}",
                    f"ip addr add {endpoint.ip}/24 dev eth0",
                    f"ip route replace default via {gateway_ip(leaf_id)}",
                ]
                machine.create_file_from_string(
                    "".join(f"nameserver {ip}\n" for ip in dns_addresses),
                    "/etc/resolv.conf",
                )
                if endpoint.name == dns.name:
                    self._configure_dns(machine, endpoint)
                    commands.append("systemctl start named")
                if endpoint.role == "web":
                    commands += [
                        "mkdir -p /var/www",
                        "dd if=/dev/zero of=/var/www/small.bin bs=1024 count=16 status=none",
                        "dd if=/dev/zero of=/var/www/large.bin bs=1M count=2 status=none",
                        *download_object_commands("/var/www"),
                        *static_http_commands(machine, docroot="/var/www"),
                    ]
                self.lab.create_file_from_list(commands, f"{endpoint.name}.startup")
            meta.design.add_interface(
                "br0", f"{gateway_ip(leaf_id)}/24", f"rack_{leaf_id}"
            )
            meta.cmd_list += [
                f"ip addr add {gateway_ip(leaf_id)}/24 dev br0",
                "ip link set br0 up",
            ]

        for binding in routing_links(self.model):
            leaf, spine = binding.leaf, binding.spine
            addresses = (binding.leaf_address, binding.spine_address)
            link = f"{spine}_{leaf}"
            for name, address, peer, peer_address in (
                (leaf, addresses[0], spine, addresses[1]),
                (spine, addresses[1], leaf, addresses[0]),
            ):
                meta = routers[name]
                self.lab.connect_machine_to_link(name, link)
                meta.cmd_list.append(
                    meta.design.add_interface(
                        f"eth{meta.eth_index}", f"{address}/31", link
                    )
                )
                meta.eth_index += 1
                meta.router_id = meta.router_id or address
                meta.neighbor_ips.append(peer_address)
                meta.design.add_neighbor(peer_address, routers[peer].AS_number)
                meta.frr_neighbor_configs.append(
                    FRR_NEIGHBOR_ADD_TEMPLATE.format(
                        neighbor_ip=peer_address, neighbor_as=routers[peer].AS_number
                    )
                )
        self.bgp_neighbors = {
            name: tuple(meta.neighbor_ips) for name, meta in routers.items()
        }
        for meta in routers.values():
            network = f"network {meta.host_network}\n" if meta.host_network else ""
            if meta.host_network:
                meta.design.bgp_networks.append(meta.host_network)
            self._write_frr_router(meta, network=network)
        from nika.net_env.synthesis.parameters import apply_routing_parameters

        apply_routing_parameters(self, routing_parameters)
        self.load_machines()
        self.desc = "Leaf-spine Clos with eBGP, one web service and client endpoints per rack. The first web endpoint also provides authoritative DNS for the fabric zone."
        self.apply_device_profile(device_profile)
        if rendered and routing_parameters is None:
            from nika.net_env.synthesis.rendered import use_rendered_configs

            use_rendered_configs(self)

    def load_machines(self):
        super().load_machines()
        self.servers["dns"] = list(self.dns_zones)

    def _configure_dns(self, machine, endpoint):
        machine.create_file_from_string(
            'options { directory "/var/cache/bind"; listen-on { any; }; allow-query { any; }; recursion no; };\n'
            'zone "fabric" { type master; file "/etc/bind/db.fabric"; };\n',
            "/etc/bind/named.conf",
        )
        records = (
            "$TTL 1H\n@ IN SOA ns.fabric. admin.fabric. (2026110801 1H 15M 1W 1D)\n"
            f"@ IN NS ns.fabric.\nns IN A {endpoint.ip}\n"
        )
        records += "".join(
            f"{e.name.replace('_', '-')} IN A {e.ip}\n"
            for e in self.model.web_endpoints()
        )
        machine.create_file_from_string(records, "/etc/bind/db.fabric")

    def bgp_hijack_targets(self):
        from nika.net_env.isp.bgp.hijack import HijackTarget

        targets = []
        for victim in self.model.web_endpoints()[1:]:
            observer = next(
                e
                for e in self.model.client_endpoints()
                if e.leaf_id not in {1, victim.leaf_id}
            )
            targets.append(
                HijackTarget(
                    hijacker=self.model.leaves[0],
                    prefix=f"10.0.{victim.leaf_id}.0/25",
                    observer=f"leaf_{observer.leaf_id}",
                    observer_host=observer.name,
                    probe_ip=victim.ip,
                )
            )
        return targets

    def router_roles(self):
        return {"spine": list(self.model.spines), "leaf": list(self.model.leaves)}

    @classmethod
    def default_probe_path(cls, *, topo_size="s", **deploy_kwargs):
        model = build_clos_fabric_model(topo_size)
        source, destination, control = model.probe_endpoints(
            deploy_kwargs.get("probe_source")
        )
        name = destination.name.replace("_", "-")
        return ProbePath(
            src_host=source.name,
            dst_ip=destination.ip,
            http_url=f"http://{destination.ip}/",
            http_name_url=f"http://{name}.fabric/",
            control_plane_host=f"leaf_{source.leaf_id}",
            peer_host=control.name,
        )

    def _bgp_peers(self):
        from nika.net_env.verify import FRR_BGP_PEERS

        native_query = FRR_BGP_PEERS
        if self.device_profile == "iosxr":
            from nika.net_env.utils.iosxr.platform import IOSXR_BGP_PEERS

            native_query = IOSXR_BGP_PEERS
        elif self.device_profile == "routeros":
            from nika.net_env.utils.routeros.platform import routeros_bgp_peer_query

            native_query = routeros_bgp_peer_query()
        return {
            router: native_query if router in self.native_routers else FRR_BGP_PEERS
            for router in self.bgp_neighbors
        }

    def _write_frr_router(self, router_meta: RouterMeta, *, network: str) -> None:
        router_meta.machine.create_file_from_path(
            str(pkg_path("net_env/utils/kathara/bgp/daemons")), "/etc/frr/daemons"
        )
        router_meta.machine.create_file_from_path(
            str(pkg_path("net_env/utils/kathara/bgp/vtysh.conf")),
            "/etc/frr/vtysh.conf",
        )
        router_meta.design.router_id = router_meta.router_id
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

    def business_workload(self):
        from nika.net_env.dc_clos.workload import build_workload

        return build_workload(
            sorted(self.client_addresses),
            [f"http://{name}/" for name in self.dns_records],
            self.frr_routers(),
            self.router_rate_limit_kbps(),
        )

    def startup_verify_lab(self) -> dict:
        if self.native_routers:
            # Native routers may defer route selection after sessions
            # establish, so readiness also requires the services.
            return self.verify_lab()
        from nika.net_env.dc_clos.verify import verify_dc_clos_lab_startup

        return verify_dc_clos_lab_startup(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            nodes=tuple(self.lab.machines),
            bgp_neighbors=self.bgp_neighbors,
            client_addresses=self.client_addresses,
        )

    def verify_lab(self) -> dict:
        from nika.net_env.dc_clos.verify import verify_dc_clos_lab

        return verify_dc_clos_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            nodes=tuple(self.lab.machines),
            bgp_neighbors=self.bgp_neighbors,
            client_addresses=self.client_addresses,
            dns_servers=self.dns_servers,
            web_urls=tuple(f"http://{name}" for name in self.dns_records),
            bgp_peers=self._bgp_peers(),
        )
