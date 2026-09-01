"""Silent, flow-dependent corruption in a forwarding device."""

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.rca.inventory import interfaces_for_node
from nika.problems.support.probe_paths import get_probe_path
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.spec import NodeRole
from traffic.burst import BurstTrafficGenerator
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf import (
    SwitchNamespaceBitflip,
)


class SwitchInternalPacketCorruptionParams(BaseModel):
    """Controller-only inputs for the deterministic switch bitflip injector."""

    forwarding_device: str = Field(description="Target forwarding device.")
    intf_name: str = Field(description="Forwarding device egress interface.")
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


class DeviceForwardingPacketCorruption(ProblemBase):
    """Inject a payload bitflip after a fabric node forwards a TCP packet."""

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "device_forwarding_packet_corruption"
    description = "A forwarding device silently corrupts selected packets."
    symptom_desc = (
        "A subset of TCP flows through one fabric switch incurs end-to-end "
        "checksum drops and retransmissions while links and BGP remain healthy."
    )
    TAGS = ["forwarding_device"]
    supported_backends = ("kathara",)
    Params = SwitchInternalPacketCorruptionParams

    def root_cause_resources(self, params: SwitchInternalPacketCorruptionParams):
        return [node_resource(params.forwarding_device)]

    def inject_fault(self, params: SwitchInternalPacketCorruptionParams) -> None:
        if params.intf_name not in interfaces_for_node(
            self.net_env, params.forwarding_device
        ):
            raise RuntimeCapabilityError(
                f"{params.intf_name!r} is not an interface on {params.forwarding_device!r}"
            )
        identity = self.net_env.machine_identities.get(params.forwarding_device)
        if identity is None or identity.role not in {NodeRole.ROUTER, NodeRole.SWITCH}:
            raise RuntimeCapabilityError(
                "device_forwarding_packet_corruption requires a router or switch target"
            )
        self._bitflip_token = SwitchNamespaceBitflip(self.runtime).attach(
            params.forwarding_device, params.intf_name, params.seed
        )
        self._start_cross_leaf_workload(params.seed)

    def verify_fault(self, params: SwitchInternalPacketCorruptionParams) -> dict:
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=SwitchNamespaceBitflip(self.runtime).attached(
                params.forwarding_device, params.intf_name
            ),
            details={
                "forwarding_device": params.forwarding_device,
                "intf": params.intf_name,
            },
        )

    def recover_fault(self, params: SwitchInternalPacketCorruptionParams) -> dict:
        injector = SwitchNamespaceBitflip(self.runtime)
        injector.detach(
            params.forwarding_device,
            params.intf_name,
            getattr(self, "_bitflip_token", None),
        )
        return {
            "verified": not injector.attached(
                params.forwarding_device, params.intf_name
            ),
            "details": {
                "forwarding_device": params.forwarding_device,
                "intf": params.intf_name,
            },
        }

    def _start_cross_leaf_workload(self, seed: int) -> None:
        """Generate stable TCP tuples on the default probe path."""
        scenario = getattr(self, "scenario_name", None) or ""
        topo_size = getattr(self.net_env, "topo_size", None) or "s"
        path = get_probe_path(scenario, topo_size=topo_size)
        if path is not None and path.peer_host and path.peer_host != path.src_host:
            BurstTrafficGenerator(self.runtime).run(
                sources=[path.src_host],
                destination=path.peer_host,
                protocol="tcp",
                rate="10M",
                packet_size=1200,
                duration=60,
                synchronized_start=0,
                seed=seed,
                flows_per_source=24,
            )
            return
        hosts = sorted(
            name
            for name, identity in self.net_env.machine_identities.items()
            if identity.role is NodeRole.HOST
        )
        if len(hosts) < 2:
            return
        BurstTrafficGenerator(self.runtime).run(
            sources=[hosts[0]],
            destination=hosts[-1],
            protocol="tcp",
            rate="10M",
            packet_size=1200,
            duration=60,
            synchronized_start=0,
            seed=seed,
            flows_per_source=24,
        )
