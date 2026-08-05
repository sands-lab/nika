import os

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase

cur_path = os.path.dirname(os.path.abspath(__file__))


class P4INT(NetworkEnvBase):
    LAB_NAME = "p4_int"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = None
    TAGS = ["link", "pc", "p4", "mac", "arp", "icmp", "int"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = "A simple P4 lab with 2 hosts, 2 leaf switches and 2 spine switches, where In-band Network Telemetry is enabled."

        pc1 = self.lab.new_machine("pc1", **{"image": "kathara/base"})
        pc2 = self.lab.new_machine("pc2", **{"image": "kathara/base"})
        collector = self.lab.new_machine("collector", **{"image": "kathara/influxdb"})

        spine1 = self.lab.new_machine("spine1", **{"image": "kathara/p4"})
        spine2 = self.lab.new_machine("spine2", **{"image": "kathara/p4"})
        leaf1 = self.lab.new_machine("leaf1", **{"image": "kathara/p4"})
        leaf2 = self.lab.new_machine("leaf2", **{"image": "kathara/p4"})

        self.lab.connect_machine_to_link(pc1.name, "A")
        self.lab.connect_machine_to_link(leaf1.name, "A")

        self.lab.connect_machine_to_link(leaf1.name, "B")
        self.lab.connect_machine_to_link(spine1.name, "B")

        self.lab.connect_machine_to_link(leaf1.name, "C")
        self.lab.connect_machine_to_link(spine2.name, "C")

        self.lab.connect_machine_to_link(pc2.name, "D")
        self.lab.connect_machine_to_link(leaf2.name, "D")

        self.lab.connect_machine_to_link(leaf2.name, "E")
        self.lab.connect_machine_to_link(spine1.name, "E")

        self.lab.connect_machine_to_link(leaf2.name, "F")
        self.lab.connect_machine_to_link(spine2.name, "F")

        self.lab.connect_machine_to_link(collector.name, "G")
        self.lab.connect_machine_to_link(leaf2.name, "G")

        # Add basic configuration to the pcs
        hosts = [pc1, pc2]
        for i, host in enumerate(hosts, start=1):
            cmd_list = [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                f"ip link set eth0 address 00:00:0a:00:00:0{i}",
                f"ip addr add 10.0.0.{i}/24 dev eth0",
            ]
            for j in range(1, len(hosts) + 2):  # +2 because collector is also included
                if j != i:
                    cmd_list.append(f"arp -s 10.0.0.{j} 00:00:0a:00:00:0{j}")

            self.lab.create_file_from_list(
                cmd_list,
                f"{host.name}.startup",
            )

        # Add basic configuration to the INT collector
        collector.copy_directory_from_path(
            os.path.join(cur_path, "collector_src"),
            "collector_src",
        )
        collector_cmd_list = [
            "sysctl net.ipv4.conf.all.arp_ignore=8",
            "sysctl net.ipv4.conf.default.arp_ignore=8",
            "sysctl net.ipv4.conf.all.arp_announce=8",
            "sysctl net.ipv4.conf.default.arp_announce=8",
            f"ip link set eth0 address 00:00:0a:00:00:0{len(hosts) + 1}",
            f"ip addr add 10.0.0.{len(hosts) + 1}/24 dev eth0",
        ]
        for j in range(1, len(hosts) + 1):
            if j != len(hosts) + 1:
                collector_cmd_list.append(f"arp -s 10.0.0.{j} 00:00:0a:00:00:0{j}")

        collector_cmd_list.append(
            "python3 collector_src/int_collector.py &> int_collector.log"
        )
        self.lab.create_file_from_list(
            collector_cmd_list,
            f"{collector.name}.startup",
        )

        # Add basic configuration to the switches
        switches = [spine1, spine2, leaf1, leaf2]
        for i, switch in enumerate(switches, start=1):
            switch.copy_directory_from_path(os.path.join(cur_path, "p4_src"), "p4_src")

            switch.create_file_from_path(
                os.path.join(cur_path, f"cmds/{switch.name}.txt"), "commands.txt"
            )

            # add the sswitch_thrift_API.py file to the Kathara image
            switch.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/p4/sswitch_thrift_API.py")),
                "/usr/local/lib/python3.11/site-packages/sswitch_thrift_API.py",
            )
            switch.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/p4/thrift_API.py")),
                "/usr/local/lib/python3.11/site-packages/thrift_API.py",
            )

        # Add startup files for the switches
        for index, switch in enumerate(switches, start=1):
            intf_num = len(self.lab.get_links_from_machines(machines=[switch.name]))
            intf_str = ""
            for i in range(intf_num):
                intf_str += f"-i {i + 1}@eth{i} "

            self.lab.create_file_from_list(
                [
                    "sysctl net.ipv4.conf.all.arp_ignore=8",
                    "sysctl net.ipv4.conf.default.arp_ignore=8",
                    "sysctl net.ipv4.conf.all.arp_announce=8",
                    "sysctl net.ipv4.conf.default.arp_announce=8",
                    "p4c p4_src/int.p4 -o p4_src/",
                    f"simple_switch {intf_str} --log-console p4_src/int.json >> sw.log &",
                    "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
                    'until simple_switch_CLI <<< "help"; do sleep 1; done',
                    "simple_switch_CLI <<< $(cat commands.txt)",
                ],
                f"{switch.name}.startup",
            )

        self.load_machines()

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.p4.p4_int.verify import verify_p4_int_lab

        return verify_p4_int_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
