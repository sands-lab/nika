"""Kubernetes fat-tree BGP lab (k8s-lab).

A two-pod fat-tree topology with BGP routing and Kubernetes (k3s) services.
The lab includes FRR routers for BGP routing and k3s nodes for Kubernetes.
"""

import os

from Kathara.manager.Kathara import Kathara
from Kathara.model.Lab import Lab

from nika.net_env.base import NetworkEnvBase

cur_path = os.path.dirname(os.path.abspath(__file__))

_FRR_IMAGE = "kathara/frr"
_K3S_IMAGE = "rancher/k3s"
_BASE_IMAGE = "kathara/base"

# K3s node ulimits required by k3s
_K3S_ULIMITS = ["nproc=65535", "nofile=65535"]


class K8sFatTreeBGP(NetworkEnvBase):
    LAB_NAME = "k8s_lab"
    VERIFY_MAX_WAIT_SEC = 1800
    VERIFY_RETRY_DELAY_SEC = 15
    TOPO_LEVEL = "hard"
    TOPO_SIZE = None
    TAGS = [
        "kubernetes",
        "k3s",
        "k8s_control_plane",
        "k8s_workload",
        "ingress",
        "metallb",
        "coredns",
        "kube_proxy",
        "k8s_storage",
        "fat-tree",
        "bgp",
        "frr",
        "link",
        "pc",
        "icmp",
        "arp",
        "mac",
    ]

    def __init__(self, **kwargs):
        super().__init__()
        self.lab = Lab(self.LAB_NAME)
        self.name = self.LAB_NAME
        self.instance = Kathara.get_instance()
        self.desc = (
            "A two-pod fat-tree topology using EBGP routing (FRR), with Kubernetes (k3s) "
            "services deployed across the cluster. The network uses leaf-spine-core architecture "
            "with BGP for routing and k3s for container orchestration."
        )
        self.kubernetes_nodes = []

        # --- FRR router machines: name -> (link order, extra_metas) ---
        # Link order determines eth index: first link = eth0, second = eth1, etc.
        _frr_machines = {
            "leaf_1_1": ["A", "B", "C", "U", "AA"],
            "leaf_1_2": ["D", "E", "V", "Z", "AB"],
            "spine_1_1": ["A", "D", "G", "H"],
            "spine_1_2": ["B", "E", "I", "J"],
            "spine_2_1": ["K", "N", "Q", "R"],
            "spine_2_2": ["L", "O", "S", "T"],
            "leaf_2_1": ["K", "L", "F"],
            "leaf_2_2": ["N", "O", "P"],
            "core_1_1": ["G", "I", "Q", "S"],
            "core_1_2": ["H", "J", "R", "T"],
            "dc_exit": ["AC", "F", "P"],
            "as1r1": ["AC", "M"],
            "as2r1": ["W", "M"],
        }

        # K3s node machines: name -> link order
        _k3s_machines = {
            "controller": ["C"],
            "worker1": ["U"],
            "worker2": ["AA"],
            "worker3": ["V"],
            "worker4": ["Z"],
            "worker5": ["AB"],
        }

        # as2r1 is bridged for internet connectivity
        _bridged = {"as2r1"}

        # Multipath sysctl for core and spine switches
        _sysctl_multipath = "net.ipv4.fib_multipath_hash_policy=1"
        _sysctl_machines = {
            "core_1_1",
            "core_1_2",
            "spine_1_1",
            "spine_1_2",
            "spine_2_1",
            "spine_2_2",
            "leaf_1_1",
            "leaf_1_2",
            "leaf_2_1",
            "leaf_2_2",
            "dc_exit",
        }

        # IPv6 required for FRR unnumbered interfaces
        _ipv6_machines = {
            "core_1_1",
            "core_1_2",
            "spine_1_1",
            "spine_1_2",
            "spine_2_1",
            "spine_2_2",
            "leaf_1_1",
            "leaf_1_2",
            "leaf_2_1",
            "leaf_2_2",
        }

        all_machines = {}

        # Create FRR router machines
        for name, links in _frr_machines.items():
            m = self.lab.new_machine(name, **{"image": _FRR_IMAGE})
            if name in _bridged:
                m.add_meta("bridged", True)
            if name in _sysctl_machines:
                m.add_meta("sysctl", _sysctl_multipath)
            if name in _ipv6_machines:
                m.add_meta("ipv6", True)
            for link in links:
                self.lab.connect_machine_to_link(name, link)
            all_machines[name] = m

        # Create k3s node machines
        for name, links in _k3s_machines.items():
            m = self.lab.new_machine(name, **{"image": _K3S_IMAGE})
            m.add_meta("privileged", True)
            for ulimit in _K3S_ULIMITS:
                m.add_meta("ulimit", ulimit)
            m.add_meta("shell", "/bin/sh")
            if name == "controller":
                m.add_meta("env", "K3S_TOKEN=secret")
                m.add_meta(
                    "args",
                    "server --disable servicelb --disable traefik --write-kubeconfig-mode 644",
                )
            else:
                m.add_meta("env", "K3S_URL=https://controller:6443")
                m.add_meta("env", "K3S_TOKEN=secret")
            for link in links:
                self.lab.connect_machine_to_link(name, link)
            all_machines[name] = m

        # Create client machine
        client = self.lab.new_machine("client", **{"image": _BASE_IMAGE})
        self.lab.connect_machine_to_link("client", "W")
        all_machines["client"] = client

        # Load per-machine configuration directories and startup scripts
        for name, m in all_machines.items():
            machine_dir = os.path.join(cur_path, name)
            if os.path.isdir(machine_dir):
                m.copy_directory_from_path(machine_dir, "/")
            startup_file = os.path.join(cur_path, f"{name}.startup")
            if os.path.isfile(startup_file):
                self.lab.create_file_from_path(startup_file, f"{name}.startup")

        # Shared k8s manifests (services, deployments) are accessible via /shared on the controller
        shared_dir = os.path.join(cur_path, "shared")
        if os.path.isdir(shared_dir):
            all_machines["controller"].copy_directory_from_path(shared_dir, "/shared")

        self.load_machines()

    def load_machines(self):
        super().load_machines()
        self.kubernetes_nodes = sorted(
            name for name, m in self.lab.machines.items() if "k3s" in m.get_image()
        )

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.kubernetes.k8s_lab.verify import verify_k8s_lab

        return verify_k8s_lab(self._build_runtime(), scenario_name=self.LAB_NAME)
