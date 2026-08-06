import os
import textwrap
from ipaddress import IPv4Interface, IPv4Network
from typing import Literal

from Kathara.manager.Kathara import Kathara, Machine
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.net_env.kathara.sdn.ovs_startup import ovs_start_commands

cur_path = os.path.dirname(os.path.abspath(__file__))


# P2P links use 172.16.0.0/16 with /31 per link.
# Host access networks use 10.<pod>.<leaf>.0/24 with leaf .1 and host .2
def assign_p2p_ips(subnet):
    base = subnet.network_address
    ip0 = IPv4Interface(f"{base}/31")
    ip1 = IPv4Interface(f"{base + 1}/31")
    return str(ip0), str(ip1)


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
        if topo_size == "s":
            SWITCH_NUM = 4  # 1 center + 3 leaf
        elif topo_size == "m":
            SWITCH_NUM = 8  # 1 center + 7 leaf
        elif topo_size == "l":
            SWITCH_NUM = 16  # 1 center + 15 leaf
        else:
            raise ValueError("topo_size should be 1, 2, or 3.")
        self.desc = textwrap.dedent("""\
        The network is an SDN star topology with one central switch and multiple edge switches.
        Each host is connected to exactly one edge switch.
        All hosts share a single access subnet 10.0.0.0/24 and receive IP addresses from this range.
        The central switch is connected to every edge switch, forming a star topology (hub-and-spoke).
        All switches also participate in a management/control network 20.0.0.0/24.
        An SDN controller runs at 20.0.0.100 and all switches are configured to use this controller via OpenFlow (tcp:20.0.0.100:6633).""")

        # add switches
        tot_switch_list = []
        for i in range(SWITCH_NUM + 1):
            switch_name = f"switch_{i}"
            switch = self.lab.new_machine(
                switch_name, **{"image": "kathara/sdn", "cpus": 0.5, "mem": "512m"}
            )
            switch_meta = SwitchMeta(
                name=switch_name,
                machine=switch,
                eth_index=0,
                cmd_list=[],
            )
            tot_switch_list.append(switch_meta)

        # add hosts
        tot_host_list = []
        for host_id in range(SWITCH_NUM):
            host_name = f"pc{host_id + 1}"
            host_machine = self.lab.new_machine(
                host_name, **{"image": "kathara/nika-base", "cpus": 0.5, "mem": "256m"}
            )
            host_meta = HostMeta(
                name=host_name,
                machine=host_machine,
                eth_index=0,
                cmd_list=[],
            )
            tot_host_list.append(host_meta)

        # add controller
        controller = self.lab.new_machine(
            "controller",
            **{
                "image": "kathara/nika-pox",
                "cpus": 0.5,
                "mem": "256m",
                "bridged": True,
            },
        )

        for switch_meta in tot_switch_list:
            switch_meta.cmd_list.extend(ovs_start_commands())
            switch_meta.cmd_list.append(f"ovs-vsctl add-br {switch_meta.name}")
            # add fail mode
            switch_meta.cmd_list.append(
                f"ovs-vsctl set-fail-mode {switch_meta.name} secure"
            )

        # connect hosts to switches
        host_pool = IPv4Network("10.0.0.0/24").hosts()
        for i in range(SWITCH_NUM):
            host_meta = tot_host_list[i]
            switch_meta = tot_switch_list[i + 1]
            self.lab.connect_machine_to_link(
                host_meta.machine.name,
                f"{host_meta.machine.name}_{switch_meta.machine.name}",
            )
            self.lab.connect_machine_to_link(
                switch_meta.machine.name,
                f"{host_meta.machine.name}_{switch_meta.machine.name}",
            )
            host_ip = str(next(host_pool))
            host_meta.cmd_list.append(f"ip addr add {host_ip}/24 dev eth0")
            host_meta.cmd_list.append("ip link set eth0 up")
            switch_meta.cmd_list.append(
                f"ovs-vsctl add-port {switch_meta.name} eth{switch_meta.eth_index}"
            )
            switch_meta.cmd_list.append(f"ip link set eth{switch_meta.eth_index} up")
            switch_meta.eth_index += 1

        center = tot_switch_list[0]  # switch_1
        for leaf in tot_switch_list[1:]:
            link_name = f"{center.machine.name}_{leaf.machine.name}"

            self.lab.connect_machine_to_link(center.machine.name, link_name)
            self.lab.connect_machine_to_link(leaf.machine.name, link_name)

            center.cmd_list.append(f"ip link set eth{center.eth_index} up")
            center.cmd_list.append(
                f"ovs-vsctl add-port {center.name} eth{center.eth_index}"
            )
            center.eth_index += 1

            leaf.cmd_list.append(f"ip link set eth{leaf.eth_index} up")
            leaf.cmd_list.append(f"ovs-vsctl add-port {leaf.name} eth{leaf.eth_index}")
            leaf.eth_index += 1

        # add controller setup
        infra_network = IPv4Network("20.0.0.0/24").hosts()
        controller_ip = "20.0.0.100"
        for switch_meta in tot_switch_list:
            switch_ip = str(next(infra_network))
            self.lab.connect_machine_to_link(
                switch_meta.machine.name, "switch_controller"
            )
            switch_meta.cmd_list.append(
                f"ip addr add {switch_ip}/24 dev eth{switch_meta.eth_index}"
            )
            switch_meta.cmd_list.append(f"ip link set eth{switch_meta.eth_index} up")
            switch_meta.cmd_list.append(
                f"ovs-vsctl set-controller {switch_meta.name} tcp:{controller_ip}:6633"
            )
            switch_meta.eth_index += 1
        self.lab.connect_machine_to_link(controller.name, "switch_controller")

        self.lab.create_file_from_list(
            [
                "ip addr add 20.0.0.100/24 dev eth0",
                "ip link set eth0 up",
                "python3 /pox/pox.py forwarding.l2_learning &",
            ],
            f"{controller.name}.startup",
        )

        # add configurations for switches
        for switch_meta in tot_switch_list:
            self.lab.create_file_from_list(
                switch_meta.cmd_list,
                f"{switch_meta.machine.name}.startup",
            )

        # add configurations for hosts
        for host_meta in tot_host_list:
            self.lab.create_file_from_list(
                host_meta.cmd_list,
                f"{host_meta.machine.name}.startup",
            )

        # load lab machines info
        self.load_machines()

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.sdn.verify import verify_sdn_star_lab

        return verify_sdn_star_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
