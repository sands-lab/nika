"""0.1.0 ``rip_small_internet_vpn``: RIP mini Internet with a WireGuard overlay."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from ipaddress import IPv4Network
from typing import Any, Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.runtime.spec import NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import (
    KATHARA_FRR_IMAGE,
    nika_image,
)

cur_path = os.path.dirname(os.path.abspath(__file__))

FRR_BASE_TEMPLATE_RIP = """
!
! FRRouting configuration file
!
!
!  RIP CONFIGURATION
!
router rip
network 192.168.0.0/16
network {network}
redistribute static
!
log file /var/log/frr/frr.log
"""

_SIZES = {"s": (2, 2, 1, 2), "m": (4, 4, 2, 4), "l": (8, 8, 4, 8)}
_ROUTER = {"image": KATHARA_FRR_IMAGE, "cpus": 0.5, "mem": "256m"}
_WG = {"image": nika_image("wireguard"), "cpus": 0.5, "mem": "256m"}


@dataclass
class _Meta:
    name: str
    machine: Machine
    eth_index: int = 0
    cmd_list: list[str] = field(default_factory=list)
    host_network: IPv4Network | None = None


def _p2p(subnet: IPv4Network) -> tuple[str, str]:
    base = subnet.network_address
    return f"{base}/31", f"{base + 1}/31"


class RIPSmallInternetVPN(NetworkEnvBase):
    LAB_NAME = "rip_small_internet_vpn"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = ["link", "http", "pc", "frr", "mac", "arp", "vpn", "icmp"]

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.topo_size = topo_size
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        if topo_size not in _SIZES:
            raise ValueError("topo_size should be one of 's', 'm', 'l'.")
        n_internal, n_hosts, n_ext_routers, n_ext_servers = _SIZES[topo_size]

        infra_pool = list(IPv4Network("192.168.0.0/16").subnets(new_prefix=31))

        def router(name: str) -> _Meta:
            self.declare_machine(
                name, role=NodeRole.ROUTER, capabilities=("linux", "frr", "rip")
            )
            return _Meta(name, self.lab.new_machine(name, **_ROUTER))

        def wg_node(name: str, role: NodeRole, **identity: Any) -> _Meta:
            self.declare_machine(
                name, role=role, capabilities=("linux", "wireguard"), **identity
            )
            return _Meta(name, self.lab.new_machine(name, **_WG))

        internal = [router(f"router{i}") for i in range(1, n_internal + 1)]
        hosts = [
            wg_node(f"pc{i}", NodeRole.HOST, reachability_target=True)
            for i in range(1, n_hosts + 1)
        ]
        gateway = router("gateway_router")
        external = [router(f"external_router_{i}") for i in range(1, n_ext_routers + 1)]
        servers: dict[str, list[_Meta]] = defaultdict(list)
        for i in range(1, n_ext_routers + 1):
            for s in range(1, n_ext_servers + 1):
                servers[f"external_router_{i}"].append(
                    wg_node(
                        f"web_server_{i}_{s}",
                        NodeRole.SERVICE,
                        service_type="web",
                        reachability_target=True,
                    )
                )
        vpn_server = wg_node("vpn_server_1", NodeRole.SERVICE, service_type="vpn")

        def p2p_link(a: _Meta, b: _Meta) -> None:
            link = f"{a.name}_{b.name}"
            self.lab.connect_machine_to_link(a.name, link)
            self.lab.connect_machine_to_link(b.name, link)
            a_ip, b_ip = _p2p(infra_pool.pop(0))
            a.cmd_list.append(f"ip addr add {a_ip} dev eth{a.eth_index}")
            b.cmd_list.append(f"ip addr add {b_ip} dev eth{b.eth_index}")
            a.eth_index += 1
            b.eth_index += 1

        for i in range(n_internal):
            for j in range(i + 1, n_internal):
                p2p_link(internal[i], internal[j])
        for r in internal[:2]:
            p2p_link(r, gateway)

        for i, (r, host) in enumerate(zip(internal, hosts)):
            link = f"{r.name}_{host.name}"
            self.lab.connect_machine_to_link(r.name, link)
            self.lab.connect_machine_to_link(host.name, link)
            subnet = IPv4Network(f"10.0.{i}.0/24")
            router_ip = subnet.network_address + 1
            r.cmd_list.append(f"ip addr add {router_ip}/24 dev eth{r.eth_index}")
            r.eth_index += 1
            r.host_network = subnet
            host.cmd_list.append(
                f"ip addr add {subnet.network_address + 2}/24 dev eth{host.eth_index}"
            )
            host.eth_index += 1
            host.cmd_list.append(f"ip route add default via {router_ip}")

        for r in external:
            p2p_link(r, gateway)

        def attach_to_bridge(r: _Meta, node: _Meta, ip: str, gw: str) -> None:
            link = f"{r.name}_{node.name}"
            self.lab.connect_machine_to_link(node.name, link)
            self.lab.connect_machine_to_link(r.name, link)
            r.cmd_list.append(f"brctl addif br0 eth{r.eth_index}")
            r.eth_index += 1
            node.cmd_list.append(f"ip addr add {ip}/24 dev eth{node.eth_index}")
            node.cmd_list.append(f"ip route add default via {gw}")
            node.eth_index += 1

        for idx, r in enumerate(external):
            r.cmd_list += ["brctl addbr br0", "ip link set dev br0 up"]
            zone = IPv4Network(f"20.0.{idx}.0/24")
            r.host_network = zone
            addrs = zone.hosts()
            gw = str(next(addrs))
            r.cmd_list.append(f"ip addr add {gw}/24 dev br0")
            if idx == 0:
                attach_to_bridge(r, vpn_server, str(next(addrs)), gw)
            for server in servers[r.name]:
                attach_to_bridge(r, server, str(next(addrs)), gw)

        for r in internal + [gateway] + external:
            r.machine.create_file_from_path(
                os.path.join(cur_path, "frr", "daemons"), "/etc/frr/daemons"
            )
            r.machine.create_file_from_path(
                os.path.join(cur_path, "frr", "vtysh.conf"), "/etc/frr/vtysh.conf"
            )
            r.machine.create_file_from_string(
                FRR_BASE_TEMPLATE_RIP.format(network=str(r.host_network)),
                "/etc/frr/frr.conf",
            )
            r.cmd_list.append("service frr start")
            self.lab.create_file_from_list(r.cmd_list, f"{r.name}.startup")

        for host in hosts:
            if host.name == "pc1":
                host.machine.copy_directory_from_path(
                    os.path.join(cur_path, "confs", "pc1"), "/"
                )
                host.cmd_list.append("wg-quick up wg0")
            self.lab.create_file_from_list(host.cmd_list, f"{host.name}.startup")

        vpn_server.machine.copy_directory_from_path(
            os.path.join(cur_path, "confs", vpn_server.name), "/"
        )
        vpn_server.cmd_list.append("wg-quick up wg0")
        self.lab.create_file_from_list(
            vpn_server.cmd_list, f"{vpn_server.name}.startup"
        )

        for r in external:
            for server in servers[r.name]:
                if server.name in ("web_server_1_1", "web_server_1_2"):
                    server.machine.copy_directory_from_path(
                        os.path.join(cur_path, "confs", server.name), "/"
                    )
                    server.cmd_list.append("wg-quick up wg0")
                    server.cmd_list.append("ping -c 3 172.16.1.1")
                server.cmd_list.append("service apache2 start")
                self.lab.create_file_from_list(
                    server.cmd_list, f"{server.name}.startup"
                )

        self.desc = (
            "A small RIP-based mini Internet with internal routers, external zones, and a VPN overlay. "
            "Internal FRR routers form a full mesh and connect to a gateway router, with internal hosts on 10.0.X.0/24 LANs (router as default gateway). "
            "The gateway connects to several external FRR routers, each fronting a bridged server LAN in 20.0.X.0/24 that hosts Apache web servers. "
            "One of them serves a WireGuard VPN server and the web servers (1_1 and 1_2) are accessible only via the VPN. "
            "All inter-router links use /31s from 192.168.0.0/16, and RIP runs on every router for both infrastructure and LAN prefixes. "
            "One internal pc (pc1) is preconfigured as a WireGuard VPN client, "
            "establishing an encrypted tunnel to the external VPN server and allowing secure access to the external web services."
        )
        self.load_machines()

    def verify_lab(self) -> dict:
        from nika.net_env.verify import (
            build_lab_verify_result,
            exec_or_empty,
            host_has_ipv4,
            http_ok,
            nodes_deployed,
            ping_ok,
            service_active,
        )

        runtime = self._build_runtime()
        expected = (
            "router1",
            "router2",
            "gateway_router",
            "external_router_1",
            "pc1",
            "pc2",
            "vpn_server_1",
            "web_server_1_1",
        )
        checks = {
            "nodes_deployed": nodes_deployed(runtime, expected),
            "router1_frr_active": service_active(runtime, "router1", "frr"),
            "pc1_ipv4": host_has_ipv4(runtime, "pc1", "10.0.0.2"),
            "pc1_gateway_reachable": ping_ok(runtime, "pc1", "10.0.0.1"),
            "pc1_to_pc2_reachable": ping_ok(runtime, "pc1", "10.0.1.2"),
            "external_web_reachable": ping_ok(runtime, "pc1", "20.0.0.3"),
            "wireguard_client": bool(
                exec_or_empty(runtime, "pc1", "wg show wg0").strip()
            ),
            "wireguard_server": bool(
                exec_or_empty(runtime, "vpn_server_1", "wg show wg0").strip()
            ),
            "web_service_active": service_active(runtime, "web_server_1_1", "apache2"),
            "web_http": http_ok(runtime, "pc1", "http://172.16.1.21/"),
        }
        return build_lab_verify_result(
            scenario_name=self.LAB_NAME,
            verified=all(checks.values()),
            checks=checks,
        )
