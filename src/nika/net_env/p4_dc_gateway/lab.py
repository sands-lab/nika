"""Kathara deployment for the P4 gateway benchmark fabric."""

from __future__ import annotations

import json
import textwrap
import time
from pathlib import Path
from typing import Literal

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.p4_dc_fabric.lab import _switch_startup
from nika.net_env.p4_dc_gateway.topology_model import (
    BASE_IMAGE,
    COLLECTOR_IMAGE,
    COLLECTOR_TELEMETRY_IP,
    CONTROLLER_IMAGE,
    CONTROLLER_OOB_IP,
    NGINX_IMAGE,
    SWITCH_IMAGE,
    VIRTUAL_ROUTER_MAC,
    GatewayFabricModel,
    TopoSize,
    build_gateway_fabric_model,
    service_gateway,
)
from nika.runtime.spec import NodeRole

_PKG = Path(__file__).resolve().parent
_MANAGER = Path(__file__).resolve().parents[1] / "utils" / "kathara" / "p4rt_manager.py"


class P4DcGateway(NetworkEnvBase):
    LAB_NAME = "p4_dc_gateway"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = [
        "link",
        "pc",
        "p4",
        "p4_runtime",
        "mac",
        "arp",
        "icmp",
        "http",
        "int",
        "telemetry",
        "flow_tracking",
        "ecn",
        "queue",
        "l4_load_balancer",
    ]
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
            Size {topo_size}: {self.model.gateway_count} gateways,
            {self.model.spine_count} spines, {self.model.leaf_count} leaves,
            {self.model.client_count} external clients, and
            {self.model.service_count} HTTP services."""
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
            machine.create_file_from_path(str(_PKG / "gateway.p4"), "gateway.p4")
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
                        "mkdir -p /var/www/html",
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
                "mkdir -p /tmp/p4_fabric /opt/nika",
                "cp p4rt_manager.py /opt/nika/p4rt_manager.py 2>/dev/null || cp /p4rt_manager.py /opt/nika/p4rt_manager.py",
                "chmod +x /opt/nika/p4rt_manager.py",
            ],
            "fabric_mgr.startup",
        )
        collector.create_file_from_path(str(_PKG / "collector.py"), "collector.py")
        self.lab.create_file_from_list(
            [
                f"ip addr add {COLLECTOR_TELEMETRY_IP}/24 dev eth0",
                "ip link set eth0 up",
                "mkdir -p /var/lib/nika",
                "python3 collector.py --output /var/lib/nika/int_reports.jsonl >/tmp/collector.log 2>&1 &",
            ],
            "collector.startup",
        )
        self.load_machines()

    def deploy(self):
        super().deploy()
        from .apply import reconcile_gateway

        reconcile_gateway(self._build_runtime(), self.model)

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

    def reconcile_dataplane_after_port_reconnect(
        self, runtime: LabRuntime, nodes: list[str]
    ) -> None:
        from .apply import reconcile_gateway

        reconcile_gateway(runtime, self.model)
