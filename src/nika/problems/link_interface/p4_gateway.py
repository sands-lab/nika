"""Link-interface failures for the P4 gateway benchmark."""

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import interface_resource
from nika.problems.support.benchmark_targets import (
    p4_gateway_port_target,
    p4_port_options,
)
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy


class SilentEgressPacketLossParams(BaseModel):
    host_name: str = Field(
        description="Gateway or spine that owns the egress interface."
    )
    intf_name: str = Field(description="Target egress interface.")
    bmv2_port: int = Field(gt=0)
    loss_basis_points: int = Field(default=200, ge=1, le=10000)
    seed: int = 42


class SilentEgressPacketLoss(ProblemBase):
    """A defective egress port silently discards a fraction of its packets.

    The loss sits past the switch pipeline, on the transmit side of the port
    (optics, SerDes, or MAC), in the hardware below anything the switch OS or
    P4Runtime can read. The switch counts the packets out, the link stays up,
    and the peer receives fewer than were sent. Only the port's transmit
    direction loses packets, at random, so only flows hashed onto that uplink
    see low-rate loss.
    """

    failure_domain = FailureDomain.LINK_INTERFACE
    symptom_probe = "custom"
    symptom_class = "gray"
    root_cause_name = "silent_egress_packet_loss"
    description = "Egress silently drops a subset of packets."
    symptom_desc = (
        "A fraction of packets disappears after one healthy egress interface."
    )
    TAGS = ["p4_runtime", "telemetry", "flow_tracking"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    supported_backends = ("kathara",)
    Params = SilentEgressPacketLossParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        ctx.rng.choice(ctx.net_env.model.services)  # keep the shared service draw
        params = p4_gateway_port_target(ctx, gateways_only=False)
        params.update(loss_basis_points="200", seed=str(ctx.seed))
        return params

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        return p4_port_options(ctx, base, gateways_only=False)

    def root_cause_resources(self, params: SilentEgressPacketLossParams):
        return [interface_resource(params.host_name, params.intf_name)]

    @staticmethod
    def loss_percent(basis_points: int) -> float:
        return basis_points / 100

    def inject_fault(self, params: SilentEgressPacketLossParams):
        # Lossy transmit path on the wire side of the port, outside every
        # lab node: the proxy drops toward the peer only.
        controller = KatharaVdeFaultProxy(self.runtime)
        self._proxy = controller.insert(params.host_name, params.intf_name)
        controller.set_netem_loss(
            self._proxy, self.loss_percent(params.loss_basis_points)
        )

    def _proxy_state(self, params: SilentEgressPacketLossParams):
        controller = KatharaVdeFaultProxy(self.runtime)
        state = getattr(self, "_proxy", None) or controller.discover(
            params.host_name, params.intf_name
        )
        return controller, state

    def verify_fault(self, params: SilentEgressPacketLossParams) -> dict:
        """Read the loss on the transmit side of the port, outside the switch."""
        controller, state = self._proxy_state(params)
        qdisc = controller.qdisc_show(state) if state is not None else ""
        expected = f"loss {self.loss_percent(params.loss_basis_points):g}%"
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=state is not None
            and state.endpoint.node == params.host_name
            and expected in qdisc,
            details={
                "interface": params.intf_name,
                "bmv2_port": params.bmv2_port,
                "loss_basis_points": params.loss_basis_points,
                "transmit_path": qdisc.strip(),
            },
        )

    def recover_fault(self, params: SilentEgressPacketLossParams) -> dict:
        controller, state = self._proxy_state(params)
        if state is not None:
            controller.remove(state)
        self._proxy = None
        return {
            "verified": controller.discover(params.host_name, params.intf_name) is None,
            "details": {"host": params.host_name, "intf": params.intf_name},
        }
