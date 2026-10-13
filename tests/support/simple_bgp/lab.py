import os

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.config import pkg_path
from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.runtime.spec import NodeRole
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image

cur_path = os.path.dirname(os.path.abspath(__file__))


class SimpleBGP(NetworkEnvBase):
    LAB_NAME = "simple_bgp"
    TOPO_LEVEL = "easy"
    TOPO_SIZE = None
    TAGS = ["arp", "link", "mac", "bgp", "icmp", "frr", "pc"]

    def target_roles(self) -> dict[str, list[str]]:
        # Link faults target the probe source host (see default_probe_path).
        return {**super().target_roles(), "hosts": ["pc1"], "host1_pool": ["pc1"]}

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        return ProbePath(
            src_host="pc1",
            dst_ip="200.1.1.2",
            control_plane_host="router1",
            peer_host="pc2",
        )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = "A simple BGP network with two routers and two pcs."

        router1 = self.lab.new_machine(
            "router1", **{"image": nika_image("frr"), "cpus": 1}
        )
        router2 = self.lab.new_machine(
            "router2", **{"image": nika_image("frr"), "cpus": 1}
        )
        for router in (router1, router2):
            self.declare_machine(
                router.name,
                role=NodeRole.ROUTER,
                capabilities=("linux", "frr", "bgp"),
            )

        pc1 = self.lab.new_machine("pc1", **{"image": nika_image("base")})
        pc2 = self.lab.new_machine("pc2", **{"image": nika_image("base")})
        for host in (pc1, pc2):
            self.declare_machine(
                host.name,
                role=NodeRole.HOST,
                capabilities=("linux",),
                reachability_target=True,
            )

        self.lab.connect_machine_to_link(router1.name, "A")
        self.lab.connect_machine_to_link(router2.name, "A")

        self.lab.connect_machine_to_link(router1.name, "B")
        self.lab.connect_machine_to_link(pc1.name, "B")

        self.lab.connect_machine_to_link(router2.name, "C")
        self.lab.connect_machine_to_link(pc2.name, "C")

        # Add basic configuration to the machines
        for i, router in enumerate([router1, router2], start=1):
            router.copy_directory_from_path(
                os.path.join(cur_path, f"router{i}/etc"), "/etc"
            )
            router.create_file_from_path(
                str(pkg_path("net_env/utils/kathara/bgp/daemons")), "/etc/frr/daemons"
            )
            router.create_file_from_path(
                str(pkg_path("net_env/utils/kathara/bgp/vtysh.conf")),
                "/etc/frr/vtysh.conf",
            )
            # to create the startup file, use self.lab instead of host
            self.lab.create_file_from_path(
                os.path.join(cur_path, f"router{i}.startup"), f"router{i}.startup"
            )

        for i, host in enumerate([pc1, pc2], start=1):
            self.lab.create_file_from_path(
                os.path.join(cur_path, f"pc{i}.startup"), f"pc{i}.startup"
            )

        # load machines
        self.load_machines()

    def verify_lab(self) -> dict:
        from tests.support.simple_bgp.verify import verify_simple_bgp_lab

        return verify_simple_bgp_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
