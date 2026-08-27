from collections import defaultdict
from pathlib import Path
from typing import Any, ClassVar, Dict, Set

from nika.runtime.base import LabRuntime
from nika.runtime.factory import runtime_for_net_env
from nika.runtime.spec import LabSpec


class NetworkEnvBase:
    LAB_NAME: ClassVar[str | None] = None
    SUPPORTED_BACKENDS: ClassVar[list[str]] = ["kathara"]
    """
    Base class for network environments."""

    def __init__(self, *, backend: str = "kathara", **kwargs):
        self.backend = backend
        self.runtime: LabRuntime | None = None
        self.topology_file: Path | None = None
        self.runtime_workdir: Path | None = None
        self.metadata: dict = {}
        self.name = None
        self.desc = None
        self.instance = None
        self.lab = None
        self.bmv2_switches = None
        self.ovs_switches = None
        self.sdn_controllers = None
        self.hosts = None
        self.routers = None
        self.links = None
        self.switches = None
        self.servers = None

    def get_lab_spec(self) -> LabSpec | None:
        """Containerlab-native scenarios may override; Kathara scenarios return None."""
        return None

    def _build_runtime(self) -> LabRuntime:
        if self.runtime is None:
            self.runtime = runtime_for_net_env(self)
        return self.runtime

    def load_machines(self):
        self.bmv2_switches = []
        self.ovs_switches = []
        self.sdn_controllers = []
        self.hosts = []
        self.routers = []
        self.switches = []
        self.servers = defaultdict(list)

        machines: Dict[str, Any] = self.lab.machines
        for machine, machine_obj in machines.items():
            image = machine_obj.get_image()
            if "p4" in image:
                self.bmv2_switches.append(machine)
            elif "frr" in image or "xrd" in image or "routeros" in image:
                self.routers.append(machine)
            elif "base" in image or "nginx" in image or "wireguard" in image:
                host_keys = ["pc", "client"]
                if any(key in machine for key in host_keys):
                    self.hosts.append(machine)
                elif "load_balancer" in machine or "lb" in machine:
                    self.servers["load_balancer"].append(machine)
                elif "switch" in machine or "sw" in machine:
                    self.switches.append(machine)
                elif "dns" in machine:
                    self.servers["dns"].append(machine)
                elif "dhcp" in machine:
                    self.servers["dhcp"].append(machine)
                elif "web" in machine and "backend" not in machine:
                    self.servers["web"].append(machine)
                elif "vpn" in machine:
                    self.servers["vpn"].append(machine)

            elif "influxdb" in image:
                self.servers["database"].append(machine)

            elif "sdn" in image:
                self.ovs_switches.append(machine)
            elif "pox" in image:
                self.sdn_controllers.append(machine)
            elif "k3s" in image:
                pass  # k8s nodes are tracked via kubernetes_nodes on kubernetes labs
            else:
                print(f"Unknown machine type: {machine} with image {image}")

        # sort all lists
        self.bmv2_switches = sorted(self.bmv2_switches)
        self.ovs_switches = sorted(self.ovs_switches)
        self.sdn_controllers = sorted(self.sdn_controllers)
        self.hosts = sorted(self.hosts)
        self.routers = sorted(self.routers)
        self.switches = sorted(self.switches)
        for server_type in self.servers:
            self.servers[server_type] = sorted(self.servers[server_type])

    def get_topology(self) -> dict:
        """
        Get the topology of the network.

        Output format: [(host1:intf1, host2:intf2), ...]
        """
        topology = defaultdict(list)
        machines = self.lab.machines
        for machine, stat in machines.items():
            for intf_num, intf in stat.interfaces.items():
                topology[intf.link.name].append(f"{machine}:eth{intf_num}")
        # sorted by the link name A, B, C, ...
        topology = sorted(topology.items(), key=lambda x: x[0])
        topo_list = []
        for link, machines in topology:
            # A link with a single endpoint (e.g. vrnetlab's reserved
            # placeholder interface) isn't a pairwise connection to report.
            if len(machines) < 2:
                continue
            topo_list.append((machines[0], machines[1]))
        return topo_list

    def get_info(self):
        """
        Generate a summary of the network configuration.
        """
        self.load_machines()
        summary = f"Network Description: {self.desc}\n"
        if self.bmv2_switches:
            summary += f"BMV2 switches: {', '.join(self.bmv2_switches)}\n"
        if self.ovs_switches:
            summary += f"OVS switches: {', '.join(self.ovs_switches)}\n"
        if self.switches:
            summary += f"Switches: {', '.join(self.switches)}\n"
        if self.hosts:
            summary += f"PCs: {', '.join(self.hosts)}\n"
        if self.servers:
            for server_type, server_list in self.servers.items():
                summary += (
                    f"{server_type.capitalize()} Servers: {', '.join(server_list)}\n"
                )
        if self.routers:
            summary += f"Routers (FRRRouting): {', '.join(self.routers)}\n"
        if self.links:
            summary += f"Links: {', '.join(self.links)}\n"
        summary += (
            f"Topology: {', '.join(f'({a}, {b})' for a, b in self.get_topology())}"
        )
        return summary

    def __str__(self):
        """
        Return a string representation of the network environment.
        """
        return self.get_info()

    def _ensure_runtime_files(self) -> None:
        if hasattr(self, "_prepare_runtime_files") and self.topology_file is None:
            self._prepare_runtime_files()

    def lab_exists(self):
        """Check if the lab exists"""
        self._ensure_runtime_files()
        return self._build_runtime().exists()

    def _collect_lab_images(self) -> Set[str]:
        if not self.lab or not self.lab.machines:
            return set()
        return {machine.get_image() for machine in self.lab.machines.values()}

    def _ensure_docker_images(self) -> None:
        """Ensure local NIKA Docker images required by this lab are available."""
        from nika.net_env.kathara.utils.docker_files.docker_images import (
            ensure_nika_docker_images,
        )

        ensure_nika_docker_images(self._collect_lab_images())

    def deploy(self):
        """Deploy the lab"""
        self._ensure_runtime_files()
        runtime = self._build_runtime()
        if runtime.exists():
            print(f"Lab {self.name} exists")
            return
        if self.backend == "kathara":
            self._ensure_docker_images()
        runtime.deploy()

    def verify_lab(self) -> dict | None:
        """Return post-deploy verification result, or ``None`` when not implemented."""
        return None

    def post_deploy(self):
        """Run once the lab is deployed and verified."""
        return

    def undeploy(self):
        """Undeploy the lab"""
        runtime = self.runtime or self._build_runtime()
        runtime.destroy()
        self.runtime = None
