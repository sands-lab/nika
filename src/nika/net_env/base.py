from collections import defaultdict
from pathlib import Path
from typing import ClassVar, Set

from nika.runtime.base import LabRuntime
from nika.runtime.factory import runtime_for_net_env
from nika.runtime.spec import LabSpec, MachineInventory, NodeIdentity, NodeRole

from nika.net_env.contract import ValidationContract


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
        self.validation_contract: ValidationContract | None = None
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
        self.machine_identities: dict[str, NodeIdentity] = {}

    def declare_machine(
        self,
        name: str,
        *,
        role: NodeRole,
        capabilities: tuple[str, ...] = (),
        service_type: str | None = None,
        reachability_target: bool = False,
    ) -> None:
        """Declare the semantic identity of a scenario machine."""
        if name in self.machine_identities:
            raise ValueError(f"Machine identity already declared: {name}")
        identity = NodeIdentity(
            role=role,
            capabilities=tuple(sorted(set(capabilities))),
            service_type=service_type,
            reachability_target=reachability_target,
        )
        self.machine_identities[name] = identity
        self.metadata["machine_identities"] = MachineInventory(
            self.machine_identities
        ).to_dict()

    def get_lab_spec(self) -> LabSpec | None:
        """Containerlab-native scenarios may override; Kathara scenarios return None."""
        return None

    def _build_runtime(self) -> LabRuntime:
        if self.runtime is None:
            self.runtime = runtime_for_net_env(self)
        return self.runtime

    def load_machines(self):
        inventory = MachineInventory(self.machine_identities)
        inventory.validate(set(self.lab.machines))
        self.machine_inventory = inventory
        self.bmv2_switches = inventory.names_for_capability("bmv2")
        self.ovs_switches = inventory.names_for_capability("ovs")
        self.sdn_controllers = inventory.names_for_role(NodeRole.CONTROLLER)
        self.hosts = inventory.names_for_role(NodeRole.HOST)
        self.routers = inventory.names_for_role(NodeRole.ROUTER)
        self.switches = inventory.names_for_role(NodeRole.SWITCH)
        self.servers = inventory.services()

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
        from agent.sandbox.sbx.images import ensure_configured_sbx_template_images
        from nika.net_env.utils.kathara.docker_files.docker_images import (
            ensure_nika_docker_images,
        )

        ensure_nika_docker_images(self._collect_lab_images())
        # When run config selects a sandbox agent, preload its sbx template
        # alongside lab images (not at sbx create / per-case agent start).
        ensure_configured_sbx_template_images()

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

    def get_validation_contract(self) -> ValidationContract | None:
        """Return the scenario's backend-independent healthy baseline contract."""
        return self.validation_contract

    def post_deploy(self):
        """Run once the lab is deployed and verified."""
        return

    def reconcile_dataplane_after_port_reconnect(
        self, runtime: LabRuntime, nodes: list[str]
    ) -> None:
        """Re-apply controller-managed forwarding after a switch port was moved."""
        return

    def preload_workload_images(self) -> None:
        """Import cached in-cluster images into k3s nodes when a cache is present."""
        from nika.net_env.utils.k8s_workload_cache import preload_workload_images

        preload_workload_images(self)

    def undeploy(self):
        """Undeploy the lab"""
        runtime = self.runtime or self._build_runtime()
        runtime.destroy()
        self.runtime = None
