"""P4Runtime-controlled L3 Clos fabric (Kathara + BMv2 simple_switch_grpc)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.p4_dc_fabric.topology_model import (
    BASE_IMAGE,
    FABRIC_CONTROLLER_IMAGE,
    FABRIC_MGR_OOB_IP,
    NGINX_IMAGE,
    SWITCH_IMAGE,
    VIRTUAL_ROUTER_MAC,
    ClosFabricModel,
    TopoSize,
    build_clos_fabric_model,
    gateway_ip,
)
from nika.runtime.spec import NodeRole

_PKG = Path(__file__).resolve().parent
_MANAGER = Path(__file__).resolve().parents[1] / "utils" / "kathara" / "p4rt_manager.py"


class SwitchMeta:
    def __init__(
        self, name: str, machine: Machine, eth_index: int, cmd_list: list[str]
    ):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list


class HostMeta:
    def __init__(
        self, name: str, machine: Machine, eth_index: int, cmd_list: list[str]
    ):
        self.name = name
        self.machine = machine
        self.eth_index = eth_index
        self.cmd_list = cmd_list
        self.ip_address: str | None = None


def _switch_startup(
    ports: list, device_id: int, oob_eth: int, oob_ip: str
) -> list[str]:
    cmds = [
        "sysctl -w net.ipv6.conf.all.disable_ipv6=1 || true",
        "sysctl -w net.ipv4.conf.all.arp_ignore=8",
        "sysctl -w net.ipv4.conf.default.arp_ignore=8",
    ]
    port_args: list[str] = []
    for port in ports:
        eth = port.name
        cmds.append(f"ip link set {eth} address {port.mac}")
        cmds.append(f"ip link set {eth} up")
        port_args.append(f"-i {port.bmv2_port}@{eth}")
    cmds.append(f"ip addr add {oob_ip}/16 dev eth{oob_eth}")
    cmds.append(f"ip link set eth{oob_eth} up")
    iface = " ".join(port_args)
    cmds.append(
        f"simple_switch_grpc {iface} --device-id {device_id} --log-console "
        f"--no-p4 -- --grpc-server-addr 0.0.0.0:9559 >> sw.log 2>&1 &"
    )
    cmds.append(
        "for i in $(seq 1 30); do pgrep -f '[s]imple_switch_grpc' && break; sleep 1; done"
    )
    return cmds


class P4DcFabric(NetworkEnvBase):
    LAB_NAME = "p4_dc_fabric"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = ["link", "pc", "p4", "p4_runtime", "mac", "arp", "icmp", "http"]
    VERIFY_MAX_WAIT_SEC = 420

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.topo_size: TopoSize = topo_size
        self.model: ClosFabricModel = build_clos_fabric_model(topo_size)
        self.web_urls = list(self.model.web_urls)
        self._host_ips = {e.name: e.ip for e in self.model.endpoints}

        self.desc = textwrap.dedent(f"""\
            Symmetric leaf-spine L3 Clos programmable fabric under P4Runtime control.
            Size {topo_size}: {self.model.spine_count} spines, {self.model.leaf_count} leaves,
            {self.model.endpoints_per_leaf} endpoints per leaf.
            Per-leaf rack prefix 10.0.<leaf>.0/24 with virtual gateway MAC {VIRTUAL_ROUTER_MAC}.
            BMv2 simple_switch_grpc; OOB control network 172.31.0.0/16;
            IPv4 LPM + ActionSelector ECMP programmed from one topology model.""")

        spine_metas: list[SwitchMeta] = []
        leaf_metas: list[SwitchMeta] = []

        for spine in self.model.spines:
            machine = self.lab.new_machine(
                spine, **{"image": SWITCH_IMAGE, "cpus": 0.5, "mem": "512m"}
            )
            self.declare_machine(
                spine,
                role=NodeRole.SWITCH,
                capabilities=("linux", "bmv2", "p4", "p4runtime"),
            )
            spine_metas.append(
                SwitchMeta(name=spine, machine=machine, eth_index=0, cmd_list=[])
            )
        for leaf in self.model.leaves:
            machine = self.lab.new_machine(
                leaf, **{"image": SWITCH_IMAGE, "cpus": 0.5, "mem": "512m"}
            )
            self.declare_machine(
                leaf,
                role=NodeRole.SWITCH,
                capabilities=("linux", "bmv2", "p4", "p4runtime"),
            )
            leaf_metas.append(
                SwitchMeta(name=leaf, machine=machine, eth_index=0, cmd_list=[])
            )

        host_metas: list[HostMeta] = []
        for ep in self.model.endpoints:
            image = NGINX_IMAGE if ep.role == "web" else BASE_IMAGE
            machine = self.lab.new_machine(
                ep.name, **{"image": image, "cpus": 0.5, "mem": "256m"}
            )
            if ep.role == "web":
                self.declare_machine(
                    ep.name,
                    role=NodeRole.SERVICE,
                    capabilities=("linux", "http"),
                    service_type="web",
                    reachability_target=True,
                )
            else:
                self.declare_machine(
                    ep.name,
                    role=NodeRole.HOST,
                    capabilities=("linux",),
                    reachability_target=True,
                )
            host_metas.append(
                HostMeta(name=ep.name, machine=machine, eth_index=0, cmd_list=[])
            )
        host_by_name = {h.name: h for h in host_metas}

        fabric_mgr = self.lab.new_machine(
            "fabric_mgr",
            **{"image": FABRIC_CONTROLLER_IMAGE, "cpus": 1, "mem": "512m"},
        )
        self.declare_machine(
            fabric_mgr.name,
            role=NodeRole.CONTROLLER,
            capabilities=("linux", "p4runtime"),
        )

        for leaf_id, leaf_meta in enumerate(leaf_metas, start=1):
            for ep in self.model.endpoints_on_leaf(leaf_id):
                host_meta = host_by_name[ep.name]
                link_name = f"{ep.name}_{leaf_meta.name}"
                self.lab.connect_machine_to_link(host_meta.machine.name, link_name)
                self.lab.connect_machine_to_link(leaf_meta.machine.name, link_name)
                gw = gateway_ip(leaf_id)
                host_meta.ip_address = ep.ip
                host_meta.cmd_list.extend(
                    [
                        f"ip link set eth{host_meta.eth_index} address {ep.mac}",
                        f"ip addr add {ep.ip}/32 dev eth{host_meta.eth_index}",
                        f"ip link set eth{host_meta.eth_index} up",
                        f"ip route replace {gw} dev eth{host_meta.eth_index}",
                        f"ip route replace default via {gw}",
                        f"ip neigh replace {gw} lladdr {VIRTUAL_ROUTER_MAC} "
                        f"dev eth{host_meta.eth_index} nud permanent",
                    ]
                )
                if ep.role == "web":
                    host_meta.cmd_list.extend(
                        [
                            "mkdir -p /var/www/html",
                            f"echo 'p4_dc_fabric web_{leaf_id}' > /var/www/html/index.html",
                            "nginx || service nginx start || true",
                        ]
                    )
                host_meta.eth_index += 1
                leaf_meta.eth_index += 1

        for leaf_meta in leaf_metas:
            for spine_meta in spine_metas:
                link_name = f"{spine_meta.name}_{leaf_meta.name}"
                self.lab.connect_machine_to_link(spine_meta.machine.name, link_name)
                self.lab.connect_machine_to_link(leaf_meta.machine.name, link_name)
                leaf_meta.eth_index += 1
                spine_meta.eth_index += 1

        for meta in spine_metas + leaf_metas:
            self.lab.connect_machine_to_link(meta.machine.name, "oob_control")
        self.lab.connect_machine_to_link(fabric_mgr.name, "oob_control")

        for meta in spine_metas + leaf_metas:
            info = self.model.switch_info[meta.name]
            ports = self.model.ports[meta.name]
            oob_eth = len(ports)
            cmds = _switch_startup(ports, info.device_id, oob_eth, info.oob_ip)
            machine = meta.machine
            machine.create_file_from_path(str(_PKG / "fabric.p4"), "fabric.p4")
            self.lab.create_file_from_list(cmds, f"{machine.name}.startup")

        fabric_mgr.create_file_from_path(str(_MANAGER), "p4rt_manager.py")
        self.lab.create_file_from_list(
            [
                f"ip addr add {FABRIC_MGR_OOB_IP}/16 dev eth0",
                "ip link set eth0 up",
                "mkdir -p /tmp/p4_fabric /opt/nika",
                "cp p4rt_manager.py /opt/nika/p4rt_manager.py 2>/dev/null || "
                "cp /p4rt_manager.py /opt/nika/p4rt_manager.py || true",
                "chmod +x /opt/nika/p4rt_manager.py || true",
            ],
            "fabric_mgr.startup",
        )

        for host_meta in host_metas:
            self.lab.create_file_from_list(
                host_meta.cmd_list, f"{host_meta.machine.name}.startup"
            )

        self.load_machines()

    def deploy(self):
        super().deploy()
        from nika.net_env.p4_dc_fabric.fabric_manager import reconcile_fabric

        reconcile_fabric(self._build_runtime(), self.model)

    def verify_lab(self) -> dict:
        from nika.net_env.p4_dc_fabric.verify import verify_p4_dc_fabric_lab

        return verify_p4_dc_fabric_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            model=self.model,
        )
