"""Kathara deployment for the P4 gateway benchmark fabric."""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path
from typing import Literal

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.net_env.probe_intent import ProbeIntent
from nika.net_env.p4_dc_fabric.fabric_manager.apply import MANAGER, MANAGER_DIR
from nika.net_env.p4_dc_fabric.lab import _switch_startup
from nika.net_env.p4_dc_gateway.topology_model import (
    BASE_IMAGE,
    CLIENT_ACCESS_MTU,
    COLLECTOR_IMAGE,
    COLLECTOR_TELEMETRY_IP,
    CONTROLLER_IMAGE,
    CONN_TABLE_CAPACITY,
    CONNECTION_LEARNING_DELAY_MS,
    POOL_UPDATE_INTERVAL_SEC,
    CONTROLLER_OOB_IP,
    LB_AGENT,
    INT_REPORTS,
    LB_AGENT_CONFIG,
    LB_AGENT_STATUS,
    NGINX_IMAGE,
    SWITCH_IMAGE,
    VIP_IP,
    VIP_PORT,
    VIRTUAL_ROUTER_MAC,
    GatewayFabricModel,
    TopoSize,
    build_gateway_fabric_model,
    service_gateway,
)
from nika.runtime.spec import NodeRole
from nika.net_env.utils.rack_workload import (
    FANOUT_PORT,
    bulk_downloads,
    fanout_processes,
    seed_for,
    web_content_commands,
)
from nika.traffic.workload import (
    FAN_IN_BURSTS,
    VIP_KEEPALIVE_SESSIONS,
    BusinessWorkload,
    WorkloadExercise,
    WorkloadProcess,
)

_PKG = Path(__file__).resolve().parent
_MANAGER = Path(__file__).resolve().parents[1] / "utils" / "kathara" / "p4rt_manager.py"
# BMv2 paths carry about 5 Mbit/s of TCP, so a 2 MB object takes a few seconds.
LARGE_OBJECT_MB = 2
# Every site agent answers a status poll with a block of datagrams sent back to
# back; BMv2 forwards such a short block far faster than its sustained rate.
# The blocks converge on the collector's leaf port at once and a normal port
# buffer absorbs them; a 16 KB queue draining at 20 Mbit/s (5.7 ms) cannot.
STATUS_ANSWER_DATAGRAMS = 24
STATUS_DATAGRAM_BYTES = 1400


class P4DcGateway(NetworkEnvBase):
    LAB_NAME = "p4_dc_gateway"
    TOPO_LEVEL = "medium"
    VERIFY_MAX_WAIT_SEC = 600

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.topo_size: TopoSize = topo_size
        self.model: GatewayFabricModel = build_gateway_fabric_model(topo_size)
        self.web_urls = self.model.web_urls
        self.desc = textwrap.dedent(
            f"""\
            P4Runtime gateway-spine-leaf fabric with 5-tuple ECMP, INT-MX,
            per-port ECN configuration, gateway flow tracking, and a minimal
            stateful L4 VIP load balancer.
            External clients attach to their gateway over a provider access
            circuit with a {CLIENT_ACCESS_MTU}-byte MTU.
            Size {topo_size}: {self.model.gateway_count} gateways,
            {self.model.spine_count} spines, {self.model.leaf_count} leaves,
            {self.model.client_count} external clients, and
            {self.model.service_count} HTTP services.
            Routing is north-south only: clients reach the services and services
            answer clients; there is no client-to-client or cross-leaf
            service-to-service path. The VIP {VIP_IP}:{VIP_PORT} fronts the
            backend pool {", ".join(b.name for b in self.model.backend_pool)};
            those backends are meant to be reached through the VIP.
            The VIP is a TCP service (port {VIP_PORT}) and does not answer ping;
            services do not originate traffic to the VIP, only external
            clients use it. Each gateway's LB agent applies routine DIP pool
            updates on its own schedule, so the lb_vip pool version can differ
            between gateways.
            Every service publishes small.bin and large.bin ({LARGE_OBJECT_MB} MB).
            Users at each client site keep browsing sessions open to the VIP,
            load small objects from the services directly, and download
            large.bin through the VIP or from a service every few minutes.
            {self._collector().name} runs a status collector: every few seconds
            it polls the agent at every client site (UDP {FANOUT_PORT}), and
            all sites answer at once with a block of datagrams."""
        )

        switches = {}
        for name in self.model.fabric_switches():
            switches[name] = self.lab.new_machine(
                name, **{"image": SWITCH_IMAGE, "cpus": 0.5, "mem": "512m"}
            )
            self.declare_machine(
                name,
                role=NodeRole.SWITCH,
                capabilities=("linux", "bmv2", "p4", "p4runtime", "telemetry"),
            )

        endpoints = {}
        for item in self.model.endpoints:
            image = NGINX_IMAGE if item.role == "http_service" else BASE_IMAGE
            endpoints[item.name] = self.lab.new_machine(
                item.name, **{"image": image, "cpus": 0.25, "mem": "256m"}
            )
            self.declare_machine(
                item.name,
                role=NodeRole.SERVICE if item.role == "http_service" else NodeRole.HOST,
                capabilities=("linux", "http")
                if item.role == "http_service"
                else ("linux",),
                service_type="web" if item.role == "http_service" else None,
                reachability_target=True,
            )

        for item in self.model.clients:
            link = f"{item.name}_{item.attached_switch}"
            self.lab.connect_machine_to_link(item.name, link)
            self.lab.connect_machine_to_link(item.attached_switch, link)
        for left, right in self.model.gateway_spine_links + self.model.spine_leaf_links:
            link = f"{left}_{right}"
            self.lab.connect_machine_to_link(left, link)
            self.lab.connect_machine_to_link(right, link)
        for item in self.model.services:
            link = f"{item.name}_{item.attached_switch}"
            self.lab.connect_machine_to_link(item.name, link)
            self.lab.connect_machine_to_link(item.attached_switch, link)

        controller = self.lab.new_machine(
            "fabric_mgr", **{"image": CONTROLLER_IMAGE, "cpus": 1, "mem": "512m"}
        )
        collector = self.lab.new_machine(
            "collector", **{"image": COLLECTOR_IMAGE, "cpus": 0.5, "mem": "256m"}
        )
        self.declare_machine(
            "fabric_mgr", role=NodeRole.CONTROLLER, capabilities=("linux", "p4runtime")
        )
        self.declare_machine(
            "collector",
            role=NodeRole.INFRASTRUCTURE,
            capabilities=("linux", "telemetry"),
        )
        for name in self.model.fabric_switches():
            self.lab.connect_machine_to_link(name, "oob_control")
            self.lab.connect_machine_to_link(name, "telemetry_lan")
        self.lab.connect_machine_to_link("fabric_mgr", "oob_control")
        self.lab.connect_machine_to_link("collector", "telemetry_lan")

        for name, machine in switches.items():
            info = self.model.switch_info[name]
            ports = self.model.ports[name]
            cmds = _switch_startup(ports, info.device_id, len(ports), info.oob_ip)
            telemetry_eth = len(ports) + 1
            cmds.insert(
                -2, f"ip addr add {info.telemetry_ip}/24 dev eth{telemetry_eth}"
            )
            cmds.insert(-2, f"ip link set eth{telemetry_eth} up")
            machine.create_file_from_path(
                str(_PKG / "hop_exporter.py"), "hop_exporter.py"
            )
            port_map = {port.name: port.bmv2_port for port in ports}
            cmds.append(
                "python3 hop_exporter.py "
                f"--switch-id {info.device_id} --role {info.role} "
                f"--collector {COLLECTOR_TELEMETRY_IP} "
                f"--ports '{json.dumps(port_map)}' "
                ">/tmp/hop-exporter.log 2>&1 &"
            )
            if info.role == "gateway":
                machine.create_file_from_path(str(_PKG / "lb_agent.py"), "lb_agent.py")
                cmds.append(
                    f"python3 {LB_AGENT} serve --device-id {info.device_id} "
                    f"--vip {VIP_IP} --vip-port {VIP_PORT} "
                    f"--learning-delay-ms {CONNECTION_LEARNING_DELAY_MS} "
                    f"--max-connections {CONN_TABLE_CAPACITY} "
                    f"--update-interval {POOL_UPDATE_INTERVAL_SEC} "
                    f"--config {LB_AGENT_CONFIG} --status {LB_AGENT_STATUS} "
                    f"--ports '{json.dumps(sorted(port_map))}' "
                    ">/tmp/lb-agent.log 2>&1 &"
                )
            self.lab.create_file_from_list(cmds, f"{name}.startup")

        for item in self.model.endpoints:
            if item.role == "http_service":
                leaf_id = int(item.attached_switch.rsplit("_", 1)[1])
                gateway = service_gateway(leaf_id)
            else:
                gateway = "192.0.2.1"
            commands = [
                f"ip link set eth0 address {item.mac}",
                f"ip addr add {item.ip}/32 dev eth0",
                "ip link set eth0 up",
                f"ip route replace {gateway} dev eth0",
                f"ip route replace default via {gateway}",
                f"ip neigh replace {gateway} lladdr {VIRTUAL_ROUTER_MAC} dev eth0 nud permanent",
            ]
            if item.role == "http_service":
                commands.extend(
                    (
                        *web_content_commands(LARGE_OBJECT_MB),
                        f"echo '{item.name}' > /var/www/html/index.html",
                        "nginx || service nginx start || true",
                    )
                )
            self.lab.create_file_from_list(commands, f"{item.name}.startup")

        controller.create_file_from_path(str(_MANAGER), "p4rt_manager.py")
        self.lab.create_file_from_list(
            [
                f"ip addr add {CONTROLLER_OOB_IP}/24 dev eth0",
                "ip link set eth0 up",
                f"mkdir -p /tmp/p4_fabric {MANAGER_DIR}",
                f"cp p4rt_manager.py {MANAGER} 2>/dev/null || cp /p4rt_manager.py {MANAGER}",
                f"chmod +x {MANAGER}",
            ],
            "fabric_mgr.startup",
        )
        collector.create_file_from_path(str(_PKG / "collector.py"), "collector.py")
        self.lab.create_file_from_list(
            [
                f"ip addr add {COLLECTOR_TELEMETRY_IP}/24 dev eth0",
                "ip link set eth0 up",
                f"mkdir -p {os.path.dirname(INT_REPORTS)}",
                f"python3 collector.py --output {INT_REPORTS} >/tmp/collector.log 2>&1 &",
            ],
            "collector.startup",
        )
        self.load_machines()

    def deploy(self):
        super().deploy()
        from .apply import reconcile_gateway

        reconcile_gateway(self._build_runtime(), self.model)

    def _collector(self):
        return self.model.backend_pool[0]

    def business_workload(self) -> BusinessWorkload:
        vip = f"http://{VIP_IP}"
        services = [f"http://{item.ip}" for item in self.model.services]
        processes: list[WorkloadProcess] = []
        for client in self.model.clients:
            seed = seed_for(client.name)
            processes += [
                WorkloadProcess(
                    node=client.name,
                    app="browser",
                    command="vip-session",
                    args=(
                        f"{vip}/",
                        f"{vip}/small.bin",
                        "--keepalive",
                        "--users",
                        "4",
                        "--pause",
                        "2-6",
                        "--seed",
                        seed,
                    ),
                ),
                # Short-lived keep-alive sessions: new VIP connections open
                # every few seconds, then are reused by later requests.
                WorkloadProcess(
                    node=client.name,
                    app="browser",
                    command="portal-session",
                    args=(
                        f"{vip}/",
                        "--keepalive",
                        "--users",
                        "4",
                        "--pause",
                        "2-6",
                        "--lifetime",
                        "10-30",
                        "--seed",
                        str(int(seed) + 1),
                    ),
                ),
                WorkloadProcess(
                    node=client.name,
                    app="browser",
                    command="browser",
                    args=(
                        *(f"{site}/small.bin" for site in services),
                        "--pause",
                        "5-20",
                        "--seed",
                        seed,
                    ),
                ),
                WorkloadProcess(
                    node=client.name,
                    app="browser",
                    command="downloader",
                    args=(
                        *(f"{site}/large.bin" for site in [vip, *services]),
                        "--pause",
                        "120-300",
                        "--seed",
                        seed,
                    ),
                ),
            ]
        collector = self._collector()
        processes += fanout_processes(
            collector.name,
            {client.name: client.ip for client in self.model.clients},
            count=STATUS_ANSWER_DATAGRAMS,
            size=STATUS_DATAGRAM_BYTES,
            period="2-4",
            worker_command="site-agent",
            aggregator_command="status-collector",
        )
        return BusinessWorkload(
            processes=tuple(processes),
            exercises=(
                WorkloadExercise(FAN_IN_BURSTS, (collector.name,)),
                *(
                    WorkloadExercise(VIP_KEEPALIVE_SESSIONS, (gateway,))
                    for gateway in self.model.gateways
                ),
                *bulk_downloads(client.name for client in self.model.clients),
            ),
        )

    def probe_intent(self) -> ProbeIntent | None:
        """North-south only: clients are mutually isolated, as are services on different leaves."""
        return ProbeIntent.from_partitions(
            [[client.name] for client in self.model.clients],
            [
                [service.name for service in self.model.services_on_leaf(leaf)]
                for leaf in self.model.leaves
            ],
        )

    def target_roles(self) -> dict[str, list[str]]:
        roles = super().target_roles()
        clients = [h for h in roles["hosts"] if "client" in h] or roles["hosts"]
        return {
            **roles,
            "hosts": clients,
            "host1_pool": clients,
            "attacker_pool": clients,
            "web": roles["web"] or clients,
            "l2_endpoints": sorted(clients, key=lambda h: h != "client_1"),
        }

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        model = build_gateway_fabric_model(
            topo_size if topo_size in {"s", "m", "l"} else "s"
        )
        return ProbePath(
            src_host=model.clients[0].name,
            dst_ip=VIP_IP,
            http_url=f"http://{VIP_IP}/",
            control_plane_host=model.clients[0].attached_switch,
            peer_host=model.clients[1].name,
            # The VIP answers TCP only; its first backend answers ICMP.
            icmp_ip=model.backend_pool[0].ip,
        )

    def startup_verify_lab(self) -> dict:
        from .verify import verify_p4_dc_gateway_lab_startup

        return verify_p4_dc_gateway_lab_startup(
            self._build_runtime(), self.LAB_NAME, self.model
        )

    def verify_lab(self) -> dict:
        from .verify import verify_p4_dc_gateway_lab

        return verify_p4_dc_gateway_lab(
            self._build_runtime(), self.LAB_NAME, self.model
        )
