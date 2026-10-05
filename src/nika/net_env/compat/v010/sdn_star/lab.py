"""0.1.0 ``sdn_star``: hub-and-spoke OVS switches under a POX controller."""

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

_EDGE_SWITCHES = {"s": 4, "m": 8, "l": 16}
_SWITCH = {"image": "kathara/sdn", "cpus": 0.5, "mem": "256m"}
_HOST = {"image": "nika/base", "cpus": 0.5, "mem": "256m"}
_CONTROLLER = {"image": "nika/pox", "cpus": 0.5, "mem": "256m", "bridged": True}


class SDNStar(NetworkEnvBase):
    LAB_NAME = "sdn_star"
    TOPO_LEVEL = "easy"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = ["link", "sdn", "pc", "mac", "arp", "icmp"]

    def __init__(self, topo_size: Literal["s", "m", "l"] = "s", **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.topo_size = topo_size
        if topo_size not in _EDGE_SWITCHES:
            raise ValueError("topo_size should be s, m, or l.")
        edge_num = _EDGE_SWITCHES[topo_size]
        self.desc = textwrap.dedent("""\
        The network is an SDN star topology with one central switch and multiple edge switches.
        Each host is connected to exactly one edge switch.
        All hosts share a single access subnet 10.0.0.0/24 and receive IP addresses from this range.
        The central switch is connected to every edge switch, forming a star topology (hub-and-spoke).
        All switches also participate in a management/control network 20.0.0.0/24.
        An SDN controller runs at 20.0.0.100 and all switches are configured to use this controller via OpenFlow (tcp:20.0.0.100:6633).""")

        switches = [f"switch_{i}" for i in range(edge_num + 1)]
        cmds: dict[str, list[str]] = {}
        eth: dict[str, int] = {}
        for sw in switches:
            self.lab.new_machine(sw, **_SWITCH)
            self.declare_machine(
                sw, role=NodeRole.SWITCH, capabilities=("linux", "ovs", "openflow")
            )
            cmds[sw] = [
                *_ovs_start_commands(),
                f"ovs-vsctl add-br {sw}",
                f"ovs-vsctl set-fail-mode {sw} secure",
            ]
            eth[sw] = 0

        self.lab.new_machine("controller", **_CONTROLLER)
        self.declare_machine(
            "controller", role=NodeRole.CONTROLLER, capabilities=("linux", "pox")
        )

        self._host_ips: dict[str, str] = {}
        host_pool = IPv4Network("10.0.0.0/24").hosts()
        for i, sw in enumerate(switches[1:], start=1):
            host = f"pc{i}"
            self.lab.new_machine(host, **_HOST)
            self.declare_machine(
                host,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )
            link = f"{host}_{sw}"
            self.lab.connect_machine_to_link(host, link)
            self.lab.connect_machine_to_link(sw, link)
            ip = str(next(host_pool))
            self._host_ips[host] = ip
            self.lab.create_file_from_list(
                [f"ip addr add {ip}/24 dev eth0"], f"{host}.startup"
            )
            cmds[sw].append(f"ovs-vsctl add-port {sw} eth{eth[sw]}")
            eth[sw] += 1

        center = switches[0]
        for leaf in switches[1:]:
            link = f"{center}_{leaf}"
            self.lab.connect_machine_to_link(center, link)
            self.lab.connect_machine_to_link(leaf, link)
            for sw in (center, leaf):
                cmds[sw] += [
                    f"ip link set eth{eth[sw]} up",
                    f"ovs-vsctl add-port {sw} eth{eth[sw]}",
                ]
                eth[sw] += 1

        infra = IPv4Network("20.0.0.0/24").hosts()
        for sw in switches:
            self.lab.connect_machine_to_link(sw, "switch_controller")
            cmds[sw] += [
                f"ip addr add {next(infra)}/24 dev eth{eth[sw]}",
                f"ovs-vsctl set-controller {sw} tcp:20.0.0.100:6633",
            ]
        self.lab.connect_machine_to_link("controller", "switch_controller")

        self.lab.create_file_from_list(
            [
                "ip addr add 20.0.0.100/24 dev eth0",
                "ip link set eth0 up",
                "python3 /pox/pox.py forwarding.l2_learning &",
            ],
            "controller.startup",
        )
        for sw in switches:
            self.lab.create_file_from_list(cmds[sw], f"{sw}.startup")

        self.load_machines()

    def verify_lab(self) -> dict:
        return verify_sdn_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            switches=("switch_0", "switch_1"),
            hosts={"pc1": self._host_ips["pc1"], "pc2": self._host_ips["pc2"]},
        )
