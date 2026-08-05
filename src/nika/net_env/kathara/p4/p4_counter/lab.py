import os

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase

cur_path = os.path.dirname(os.path.abspath(__file__))


class P4Counter(NetworkEnvBase):
    LAB_NAME = "p4_counter"
    TOPO_LEVEL = "easy"
    TOPO_SIZE = None
    TAGS = ["link", "pc", "p4", "mac", "arp", "icmp"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = "A simple network with 4 bmv2 switches and 3 pcs."

        pc1 = self.lab.new_machine("pc1", **{"image": "kathara/base"})
        pc2 = self.lab.new_machine("pc2", **{"image": "kathara/base"})
        pc3 = self.lab.new_machine("pc3", **{"image": "kathara/base"})

        s1 = self.lab.new_machine("s1", **{"image": "kathara/p4"})
        s2 = self.lab.new_machine("s2", **{"image": "kathara/p4"})
        s3 = self.lab.new_machine("s3", **{"image": "kathara/p4"})
        s4 = self.lab.new_machine("s4", **{"image": "kathara/p4"})

        self.lab.connect_machine_to_link(pc1.name, "A", 0)
        self.lab.connect_machine_to_link(s1.name, "A", 0)

        self.lab.connect_machine_to_link(s1.name, "B", 1)
        self.lab.connect_machine_to_link(s2.name, "B", 0)

        self.lab.connect_machine_to_link(s1.name, "C", 2)
        self.lab.connect_machine_to_link(s3.name, "C", 0)

        self.lab.connect_machine_to_link(s2.name, "D", 1)
        self.lab.connect_machine_to_link(s4.name, "D", 0)

        self.lab.connect_machine_to_link(s3.name, "E", 1)
        self.lab.connect_machine_to_link(s4.name, "E", 1)

        self.lab.connect_machine_to_link(pc2.name, "F", 0)
        self.lab.connect_machine_to_link(s4.name, "F", 2)

        self.lab.connect_machine_to_link(pc3.name, "G", 0)
        self.lab.connect_machine_to_link(s4.name, "G", 3)

        # Add basic configuration to the machines
        for i in range(1, 4):
            cmd_list = [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                f"ip link set eth0 address 00:00:0a:00:00:0{i}",
                f"ip addr add 10.0.0.{i}/24 dev eth0",
            ]
            for j in range(1, 4):
                if j != i:
                    cmd_list.append(f"arp -s 10.0.0.{j} 00:00:0a:00:00:0{j}")

            self.lab.create_file_from_list(
                cmd_list,
                f"pc{i}.startup",
            )

        for i, s_i in enumerate([s1, s2, s3, s4], start=1):
            s_i.create_file_from_path(
                os.path.join(cur_path, "p4_src/l2_basic_forwarding_counter.p4"),
                "l2_basic_forwarding_counter.p4",
            )
            s_i.create_file_from_path(
                os.path.join(cur_path, f"cmds/s{i}.txt"), "commands.txt"
            )

            # add the sswitch_thrift_API.py file to the Kathara image
            s_i.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/p4/sswitch_thrift_API.py")),
                "/usr/local/lib/python3.11/site-packages/sswitch_thrift_API.py",
            )
            s_i.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/p4/thrift_API.py")),
                "/usr/local/lib/python3.11/site-packages/thrift_API.py",
            )

        self.lab.create_file_from_list(
            [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                "p4c l2_basic_forwarding_counter.p4",
                "simple_switch -i 1@eth0 -i 2@eth1 -i 3@eth2 --log-console l2_basic_forwarding_counter.json >> sw.log &",
                "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
                'until simple_switch_CLI <<< "help"; do sleep 1; done',
                "simple_switch_CLI <<< $(cat commands.txt)",
            ],
            "s1.startup",
        )

        self.lab.create_file_from_list(
            [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                "p4c l2_basic_forwarding_counter.p4",
                "simple_switch -i 1@eth0 -i 2@eth1 --log-console l2_basic_forwarding_counter.json >> sw.log &",
                "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
                'until simple_switch_CLI <<< "help"; do sleep 1; done',
                "simple_switch_CLI <<< $(cat commands.txt)",
            ],
            "s2.startup",
        )

        self.lab.create_file_from_list(
            [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                "p4c l2_basic_forwarding_counter.p4",
                "simple_switch -i 1@eth0 -i 2@eth1 --log-console l2_basic_forwarding_counter.json >> sw.log &",
                "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
                'until simple_switch_CLI <<< "help"; do sleep 1; done',
                "simple_switch_CLI <<< $(cat commands.txt)",
            ],
            "s3.startup",
        )

        self.lab.create_file_from_list(
            [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                "p4c l2_basic_forwarding_counter.p4",
                "simple_switch -i 1@eth0 -i 2@eth1 -i 3@eth2 -i 4@eth3 --log-console l2_basic_forwarding_counter.json >> sw.log &",
                "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
                'until simple_switch_CLI <<< "help"; do sleep 1; done',
                "simple_switch_CLI <<< $(cat commands.txt)",
            ],
            "s4.startup",
        )

        # load machines
        self.load_machines()

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.p4.p4_counter.verify import verify_p4_counter_lab

        return verify_p4_counter_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
