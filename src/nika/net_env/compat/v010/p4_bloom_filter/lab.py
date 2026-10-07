import os

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase
from nika.runtime.spec import NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import (
    KATHARA_BASE_IMAGE,
    KATHARA_P4_IMAGE,
)

cur_path = os.path.dirname(os.path.abspath(__file__))


class P4BloomFilter(NetworkEnvBase):
    LAB_NAME = "p4_bloom_filter"
    TOPO_LEVEL = "easy"
    TOPO_SIZE = None
    TAGS = ["link", "pc", "p4", "mac", "arp", "icmp", "bloom_filter"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = "A simple network with 2 bmv2 switches and 2 pcs implementing a bloom filter."

        pc1 = self.lab.new_machine("pc1", **{"image": KATHARA_BASE_IMAGE})
        pc2 = self.lab.new_machine("pc2", **{"image": KATHARA_BASE_IMAGE})

        switch_1 = self.lab.new_machine("switch_1", **{"image": KATHARA_P4_IMAGE})
        switch_2 = self.lab.new_machine("switch_2", **{"image": KATHARA_P4_IMAGE})
        for name in (pc1.name, pc2.name):
            self.declare_machine(name, role=NodeRole.HOST, capabilities=("linux",))
        for name in (switch_1.name, switch_2.name):
            self.declare_machine(
                name, role=NodeRole.SWITCH, capabilities=("linux", "bmv2", "p4")
            )

        self.lab.connect_machine_to_link(pc1.name, "A", 0)
        self.lab.connect_machine_to_link(switch_1.name, "A", 0)

        self.lab.connect_machine_to_link(pc2.name, "B", 0)
        self.lab.connect_machine_to_link(switch_2.name, "B", 0)

        self.lab.connect_machine_to_link(switch_1.name, "C", 1)
        self.lab.connect_machine_to_link(switch_2.name, "C", 1)

        # Add basic configuration to the machines
        for i in range(1, 3):
            cmd_list = [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                f"ip link set eth0 address 00:00:0a:00:00:0{i}",
                f"ip addr add 10.0.0.{i}/24 dev eth0",
            ]
            for j in range(1, 3):
                if j != i:
                    cmd_list.append(f"arp -s 10.0.0.{j} 00:00:0a:00:00:0{j}")

            self.lab.create_file_from_list(
                cmd_list,
                f"pc{i}.startup",
            )

        for i, s_i in enumerate([switch_1, switch_2], start=1):
            s_i.create_file_from_path(
                os.path.join(cur_path, "bloom_filter.p4"),
                "bloom_filter.p4",
            )
            s_i.create_file_from_path(
                os.path.join(cur_path, f"cmds/switch_{i}.txt"), "commands.txt"
            )

        self.lab.create_file_from_list(
            [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                "p4c bloom_filter.p4",
                "simple_switch -i 1@eth0 -i 2@eth1 bloom_filter.json >> sw.log &",
                "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
                "max_tries=5; count=0; "
                'until simple_switch_CLI <<< "help"; do '
                "  sleep 1; "
                "  count=$((count+1)); "
                '  if [ "$count" -ge "$max_tries" ]; then '
                '    echo "simple_switch_CLI not ready after $max_tries attempts" >&2; '
                "    exit 1; "
                "  fi; "
                "done",
                "simple_switch_CLI <<< $(cat commands.txt)",
            ],
            "switch_1.startup",
        )

        self.lab.create_file_from_list(
            [
                "sysctl net.ipv4.conf.all.arp_ignore=8",
                "sysctl net.ipv4.conf.default.arp_ignore=8",
                "sysctl net.ipv4.conf.all.arp_announce=8",
                "sysctl net.ipv4.conf.default.arp_announce=8",
                "p4c bloom_filter.p4",
                "simple_switch -i 1@eth0 -i 2@eth1 bloom_filter.json >> sw.log &",
                "while [[ $(pgrep simple_switch) -eq 0 ]]; do sleep 1; done",
                "max_tries=5; count=0; "
                'until simple_switch_CLI <<< "help"; do '
                "  sleep 1; "
                "  count=$((count+1)); "
                '  if [ "$count" -ge "$max_tries" ]; then '
                '    echo "simple_switch_CLI not ready after $max_tries attempts" >&2; '
                "    exit 1; "
                "  fi; "
                "done",
                "simple_switch_CLI <<< $(cat commands.txt)",
            ],
            "switch_2.startup",
        )

        # load machines
        self.load_machines()

    def verify_lab(self) -> dict:
        from nika.net_env.compat.v010.p4_bloom_filter.verify import (
            verify_p4_bloom_filter_lab,
        )

        return verify_p4_bloom_filter_lab(
            self._build_runtime(), scenario_name=self.LAB_NAME
        )
