from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Set

from nika.runtime.base import LabRuntime
from nika.runtime.factory import runtime_for_net_env
from nika.runtime.spec import LabSpec, MachineInventory, NodeIdentity, NodeRole

from nika.net_env.contract import ValidationContract
from nika.net_env.probe_intent import ProbeIntent

if TYPE_CHECKING:
    from nika.traffic.workload import BusinessWorkload


@dataclass(frozen=True)
class ProbePath:
    """Default traffic endpoints for a scenario (inject host pools / probes)."""

    src_host: str
    dst_ip: str | None = None
    http_url: str | None = None
    symptom_url: str | None = None
    control_url: str | None = None
    control_plane_host: str | None = None
    ping_count: int = 20
    gray_ping_count: int = 100
    http_name_url: str | None = None
    peer_host: str | None = None
    old_ip: str | None = None
    # An address that answers ICMP on the way to ``dst_ip`` when ``dst_ip`` is
    # an L4-only service address such as a load-balancer VIP.
    icmp_ip: str | None = None


class NetworkEnvBase:
    ROUTING_PARAMETER_KINDS: ClassVar[frozenset[str]] = frozenset()
    ROUTING_MODELS: ClassVar[frozenset[str]] = frozenset()

    LAB_NAME: ClassVar[str | None] = None
    # Kubernetes labs (``NetEnvSpec.k8s_image_cache``): lab node images pulled
    # on the host, and in-cluster images cached as tars and loaded into k3s.
    K8S_HOST_IMAGES: ClassVar[tuple[str, ...]] = ()
    K8S_WORKLOAD_IMAGES: ClassVar[tuple[str, ...]] = ()
    # Routers route only frames addressed to their own interface MAC, as real
    # L3 devices do (scenario-declared forwarding behaviour).
    ROUTERS_CHECK_DST_MAC: ClassVar[bool] = False
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

    def bgp_import_peers(self):
        """Neighbors in the fixed routing sketch available for policy completion."""
        return ()

    def bgp_origin_prefixes(self):
        """Exact prefix ownership in the fixed BGP configuration sketch."""
        import re

        from nika.net_env.native_routers import router_sketches

        origins = {}
        for node, (config, _) in router_sketches(self).items():
            for prefix in re.findall(r"^\s*network ([0-9./]+)$", config, re.MULTILINE):
                if prefix in origins:
                    raise ValueError("The BGP sketch requires one origin per prefix")
                origins[prefix] = node
        return origins

    def overlay_interfaces(self) -> list[tuple[str, str]]:
        """``(node, interface)`` pairs created at startup rather than by lab links.

        Tunnel interfaces can carry root causes, so the submission catalog must
        list them alongside link termination points.
        """
        return []

    def target_roles(self) -> dict[str, list[str]]:
        """Scenario node pools keyed by stable target-role name.

        Fault-target resolution reads these instead of matching scenario names.
        Requires a loaded inventory (``load_machines`` on a live or offline
        lab); the default derives every role from that generic inventory and
        scenarios override roles whose members their topology constrains.

        Role keys (each value is a list of node names):

        - ``hosts`` / ``host1_pool``: end hosts eligible as fault targets.
        - ``routers``: routers eligible as router-level fault targets.
        - ``web``: HTTP servers (or the hosts standing in for them).
        - ``attacker_pool``: hosts eligible to source attack traffic.
        - ``controllers``: SDN / control-plane nodes.
        - ``k8s_nodes`` / ``k8s_controllers``: Kubernetes nodes / control plane.
        - ``access_routers``: routers with attached end hosts.
        - ``bgp_originators``: routers that originate BGP ``network`` prefixes.

        Optional roles, absent unless the scenario declares them:

        - ``edges``: site edge routers.
        - ``l2_endpoints``: probe-path hosts that anchor L2 (ARP) endpoint
          faults, preferred first. When absent, any host may be used.
        """
        hosts = list(self.hosts or [])
        routers = list(self.routers or [])
        k8s_nodes = list(getattr(self, "kubernetes_nodes", None) or [])
        # Clos-style naming: spines only peer and carry no end hosts or
        # ``network`` statements, so prefer leaves when the lab names them.
        access_routers = [r for r in routers if "leaf" in r] or routers
        return {
            "hosts": hosts,
            "host1_pool": hosts,
            "routers": routers,
            "web": list((self.servers or {}).get("web") or []),
            "attacker_pool": hosts,
            "controllers": list(self.sdn_controllers or []),
            "k8s_nodes": k8s_nodes,
            "k8s_controllers": [n for n in k8s_nodes if "controller" in n] or k8s_nodes,
            "access_routers": access_routers,
            "bgp_originators": access_routers,
        }

    @classmethod
    def default_probe_path(
        cls, *, topo_size: str = "s", **deploy_kwargs
    ) -> ProbePath | None:
        """Default traffic endpoints probed by injects and symptom checks.

        ``deploy_kwargs`` are the scenario's deploy defaults (e.g. ISP ``topo``).
        Returns ``None`` when the scenario declares no default path.
        """
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

    def _topology_segments(self) -> list[tuple[str, ...]]:
        """Return every interface on each shared collision domain."""
        from nika.telemetry import is_telemetry_link

        topology = defaultdict(list)
        machines = self.lab.machines
        for machine, stat in machines.items():
            for intf_num, intf in stat.interfaces.items():
                # The telemetry management network is out-of-band, not a scenario link.
                if is_telemetry_link(intf.link.name):
                    continue
                topology[intf.link.name].append(f"{machine}:eth{intf_num}")
        # sorted by the link name A, B, C, ...
        topology = sorted(topology.items(), key=lambda x: x[0])
        topo_list = []
        for link, machines in topology:
            # A link with a single endpoint (e.g. vrnetlab's reserved
            # placeholder interface) isn't a pairwise connection to report.
            if len(machines) < 2:
                continue
            topo_list.append(tuple(machines))
        return topo_list

    def get_topology(self) -> list[tuple[str, str]]:
        """Return the representative endpoint pair used by link consumers."""
        return [(segment[0], segment[1]) for segment in self._topology_segments()]

    @staticmethod
    def _platform_label(capabilities: Iterable[str]) -> str:
        caps = set(capabilities)
        if "routeros" in caps:
            return "MikroTik RouterOS"
        if "iosxr" in caps:
            return "IOS-XR"
        if "nokia_srlinux" in caps or "srlinux" in caps:
            return "Nokia SR Linux"
        if "frr" not in caps and "linux" in caps:
            return "Linux, static routes"
        return "FRRRouting"

    def frr_routers(self) -> list[str]:
        """Routers declared as FRR (they run mgmtd and keep a commit history)."""
        return sorted(
            name
            for name, identity in self.machine_identities.items()
            if identity.role is NodeRole.ROUTER and "frr" in identity.capabilities
        )

    def _router_platform_groups(self) -> dict[str, list[str]]:
        """Routers grouped by the platform their declared capabilities imply."""
        groups: dict[str, list[str]] = {}
        for name in self.routers or []:
            identity = self.machine_identities.get(name)
            label = self._platform_label(identity.capabilities if identity else ())
            groups.setdefault(label, []).append(name)
        return groups

    def _format_role_inventory_lines(self) -> str:
        """Role / service inventory lines for the agent-facing network brief."""
        lines: list[str] = []
        if self.bmv2_switches:
            lines.append(f"BMV2 switches: {', '.join(self.bmv2_switches)}")
        if self.ovs_switches:
            lines.append(f"OVS switches: {', '.join(self.ovs_switches)}")
        if self.switches:
            lines.append(f"Switches: {', '.join(self.switches)}")
        if self.hosts:
            lines.append(f"PCs: {', '.join(self.hosts)}")
        if self.servers:
            for server_type, server_list in self.servers.items():
                lines.append(
                    f"{server_type.capitalize()} Servers: {', '.join(server_list)}"
                )
        for label, names in self._router_platform_groups().items():
            lines.append(f"Routers ({label}): {', '.join(names)}")
        if self.links:
            lines.append(f"Links: {', '.join(self.links)}")
        return "".join(f"{line}\n" for line in lines)

    def get_info(self):
        """
        Generate a summary of the network configuration.

        Agent brief contract: short description, role inventory, and topology
        edges. Device configs and fault ground truth stay outside this prompt.
        """
        self.load_machines()
        summary = f"Network Description: {self.desc}\n"
        summary += self._format_role_inventory_lines()
        summary += f"Topology: {', '.join('(' + ', '.join(segment) + ')' for segment in self._topology_segments())}"
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
        from nika.net_env.utils.kathara.docker_files.docker_images import (
            ensure_nika_docker_images,
        )

        ensure_nika_docker_images(self._collect_lab_images())

    def _has_readiness_verifier(self) -> bool:
        cls = type(self)
        if getattr(cls, "startup_verify_lab", None) is not None:
            return True
        return cls.verify_lab is not NetworkEnvBase.verify_lab

    def deploy(self):
        """Deploy the lab.

        The runtime checks for an existing lab and, on Kathara, required
        images once per deploy. After a fresh deploy, environments without
        ``startup_verify_lab`` or ``verify_lab`` wait ``deploy_settle_sec``.
        Environments that implement either skip the delay; callers poll
        readiness instead.
        """
        self._ensure_runtime_files()
        deployed = self._build_runtime().deploy()
        if deployed is False or self._has_readiness_verifier():
            return
        import time

        from nika.runtime.shared.settings import lab_settings

        time.sleep(lab_settings().deploy_settle_sec)

    def verify_lab(self) -> dict | None:
        """Return post-deploy verification result, or ``None`` when not implemented."""
        return None

    def get_validation_contract(self) -> ValidationContract | None:
        """Return the scenario's backend-independent healthy baseline contract."""
        return self.validation_contract

    def probe_intent(self) -> ProbeIntent | None:
        """Designed isolation and RTT expectations for fleet-wide reachability probes.

        Scenarios whose healthy baseline intentionally isolates endpoint pairs or
        adds WAN latency override this; the default declares nothing.
        """
        return None

    def post_deploy(self):
        """Run once the lab is deployed and verified."""
        return

    def business_workload(self) -> "BusinessWorkload | None":
        """Always-on traffic of the scenario's users and services.

        Return ``None`` for a quiet lab. The workload must be symmetric across
        cases: it never depends on a fault. See :mod:`nika.traffic.workload`.
        """
        return None

    def start_business_workload(self) -> "BusinessWorkload | None":
        """Start ``business_workload()`` on the deployed lab (idempotent)."""
        workload = self.business_workload()
        if workload is None or not workload.processes:
            return None
        from nika.traffic.workload import start_workload

        start_workload(self._build_runtime(), workload)
        return workload

    @classmethod
    def prepare_k8s_image_cache(cls) -> None:
        """Stage extra host-side artifacts when the k8s image cache is filled."""

    def preload_workload_images(self) -> None:
        """Import cached in-cluster images into k3s nodes when a cache is present."""
        from nika.net_env.utils.k8s_workload_cache import preload_workload_images

        preload_workload_images(self)

    def undeploy(self):
        """Undeploy the lab"""
        runtime = self.runtime or self._build_runtime()
        runtime.destroy()
        self.runtime = None
