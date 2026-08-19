import os

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase
from nika.runtime.spec import NodeRole

cur_path = os.path.dirname(os.path.abspath(__file__))


class P4_MPLS(NetworkEnvBase):
    LAB_NAME = "p4_mpls"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = None
    TAGS = ["link", "pc", "p4", "mac", "arp", "icmp", "mpls"]

    def _add_link(self, device_a: str, device_b: str):
        self.lab.connect_machine_to_link(device_a, f"{device_a}_to_{device_b}")
        self.lab.connect_machine_to_link(device_b, f"{device_a}_to_{device_b}")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = "A MPLS network using Bmv2 switches"

        pc1 = self.lab.new_machine("pc1", **{"image": "kathara/base"})
        pc2 = self.lab.new_machine("pc2", **{"image": "kathara/base"})
        pc3 = self.lab.new_machine("pc3", **{"image": "kathara/base"})
        for host in (pc1, pc2, pc3):
            self.declare_machine(
                host.name,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )

        switches = {}
        for i in range(1, 8):
            switch = self.lab.new_machine(
                f"switch_{i}",
                **{"image": "kathara/p4", "cpus": 0.5, "mem": "256m"},
            )
            self.declare_machine(
                switch.name,
                role=NodeRole.SWITCH,
                capabilities=("linux", "bmv2", "p4"),
            )
            switches[f"switch_{i}"] = switch

        self._add_link(pc1.name, "switch_1")
        self._add_link("switch_1", "switch_2")
        self._add_link("switch_1", "switch_3")
        self._add_link("switch_2", "switch_4")
        self._add_link("switch_3", "switch_4")
        self._add_link("switch_4", "switch_5")
        self._add_link("switch_4", "switch_6")
        self._add_link("switch_5", "switch_7")
        self._add_link("switch_6", "switch_7")
        self._add_link("switch_7", pc2.name)
        self._add_link("switch_7", pc3.name)

        # Add basic configuration to the machines
        for i in range(1, 4):
            self.lab.create_file_from_path(
                os.path.join(cur_path, f"startups/pc{i}.startup"),
                f"pc{i}.startup",
            )
        for i in range(1, 8):
            self.lab.create_file_from_path(
                os.path.join(cur_path, f"startups/switch_{i}.startup"),
                f"switch_{i}.startup",
            )

        # add cmds
        for i in range(1, 8):
            sw = switches[f"switch_{i}"]
            sw.create_file_from_path(
                os.path.join(cur_path, "mpls.p4"),
                "mpls.p4",
            )
            sw.create_file_from_path(
                os.path.join(cur_path, f"cmds/switch_{i}/commands.txt"), "commands.txt"
            )

            # add the switch_thrift_API.py file to the Kathara image
            sw.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/p4/sswitch_thrift_API.py")),
                "/usr/local/lib/python3.11/site-packages/sswitch_thrift_API.py",
            )
            sw.create_file_from_path(
                str(pkg_path("net_env/kathara/utils/p4/thrift_API.py")),
                "/usr/local/lib/python3.11/site-packages/thrift_API.py",
            )

        # load machines
        self.load_machines()

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.p4.p4_mpls.verify import verify_p4_mpls_lab

        return verify_p4_mpls_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
