"""0.1.0 ``sdn_clos``: L2 spine-leaf OVS fabric under a POX controller."""

from __future__ import annotations

import textwrap
from ipaddress import IPv4Network
from typing import Literal

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.compat.v010.sdn_verify import verify_sdn_lab
from nika.net_env.base import NetworkEnvBase
from nika.net_env.sdn_l3_clos.l3_clos_topo import _ovs_start_commands
from nika.runtime.spec import NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import (
    KATHARA_SDN_IMAGE,
    nika_image,
)

_SIZES = {"s": (1, 2, 2), "m": (2, 4, 4), "l": (4, 8, 8)}
_SWITCH = {"image": KATHARA_SDN_IMAGE, "cpus": 0.5, "mem": "256m"}
_HOST = {"image": nika_image("base"), "cpus": 0.5, "mem": "256m"}
_CONTROLLER = {"image": nika_image("pox"), "cpus": 0.5, "mem": "256m", "bridged": True}


class SDNClos(NetworkEnvBase):
    LAB_NAME = "sdn_clos"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = ["link", "sdn", "pc", "mac", "arp", "icmp"]

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.topo_size = topo_size
        if topo_size not in _SIZES:
            raise ValueError("topo_size should be s, m, or l.")
        spine_num, leaf_num, host_per_leaf = _SIZES[topo_size]

        self.desc = textwrap.dedent("""\
            This experiment uses a scalable SDN spine–leaf topology whose size depends on the selected topo_size.
            Each leaf switch connects to all spine switches using point-to-point links.
            Each leaf switch connects to two hosts belong to the same subnet 10.0.0.0/24.
            All switches also join a management network 20.0.0.0/24.
            The SDN controller resides at 20.0.0.100 and manages all switches via OpenFlow.""")

        spines = [f"spine_{i + 1}" for i in range(spine_num)]
        leaves = [f"leaf_{i + 1}" for i in range(leaf_num)]
        switches = spines + leaves
        cmds: dict[str, list[str]] = {}
        eth: dict[str, int] = {}
        for name in switches:
            self.lab.new_machine(name, **_SWITCH)
            self.declare_machine(
                name, role=NodeRole.SWITCH, capabilities=("linux", "ovs", "openflow")
            )
            cmds[name] = [
                *_ovs_start_commands(),
                f"ovs-vsctl add-br {name}",
                f"ovs-vsctl set-fail-mode {name} secure",
            ]
            eth[name] = 0

        self._host_ips: dict[str, str] = {}
        host_pool = IPv4Network("10.0.0.0/24").hosts()
        for leaf_idx, leaf in enumerate(leaves, start=1):
            for host_idx in range(1, host_per_leaf + 1):
                host = f"pc_{leaf_idx}_{host_idx}"
                self.lab.new_machine(host, **_HOST)
                self.declare_machine(
                    host,
                    role=NodeRole.HOST,
                    capabilities=("linux",),
                    reachability_target=True,
                )
                link = f"{host}_{leaf}"
                self.lab.connect_machine_to_link(host, link)
                self.lab.connect_machine_to_link(leaf, link)
                ip = str(next(host_pool))
                self._host_ips[host] = ip
                self.lab.create_file_from_list(
                    [f"ip addr add {ip}/24 dev eth0", "ip link set eth0 up"],
                    f"{host}.startup",
                )
                cmds[leaf] += [
                    f"ovs-vsctl add-port {leaf} eth{eth[leaf]}",
                    f"ip link set eth{eth[leaf]} up",
                ]
                eth[leaf] += 1

        for spine in spines:
            for leaf in leaves:
                link = f"{spine}_{leaf}"
                self.lab.connect_machine_to_link(spine, link)
                self.lab.connect_machine_to_link(leaf, link)
                for sw in (spine, leaf):
                    cmds[sw].append(f"ovs-vsctl add-port {sw} eth{eth[sw]}")
                    eth[sw] += 1

        self.lab.new_machine("controller", **_CONTROLLER)
        self.declare_machine(
            "controller", role=NodeRole.CONTROLLER, capabilities=("linux", "pox")
        )
        infra = IPv4Network("20.0.0.0/24").hosts()
        for sw in switches:
            self.lab.connect_machine_to_link(sw, "switch_controller")
            cmds[sw] += [
                f"ip addr add {next(infra)}/24 dev eth{eth[sw]}",
                f"ip link set eth{eth[sw]} up",
                f"ovs-vsctl set-controller {sw} tcp:20.0.0.100:6633",
            ]
            eth[sw] += 1
        self.lab.connect_machine_to_link("controller", "switch_controller")

        # m/l fabrics have multiple spines, so the leaf-spine mesh contains
        # loops; discovery + spanning_tree keeps floods on a loop-free tree.
        self.lab.create_file_from_list(
            [
                "ip addr add 20.0.0.100/24 dev eth0",
                "ip link set eth0 up",
                "python3 /pox/pox.py openflow.discovery openflow.spanning_tree"
                " --no-flood --hold-down forwarding.l2_learning &",
            ],
            "controller.startup",
        )
        for sw in switches:
            self.lab.create_file_from_list(cmds[sw], f"{sw}.startup")

        self.load_machines()

    def verify_lab(self) -> dict:
        ips = self._host_ips
        return verify_sdn_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            switches=("spine_1", "leaf_1", "leaf_2"),
            hosts={"pc_1_1": ips["pc_1_1"], "pc_2_1": ips["pc_2_1"]},
        )
