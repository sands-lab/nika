"""Link-interface failures for the P4 gateway benchmark."""

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import interface_resource
from nika.problems.support.p4_gateway import set_deterministic_loss


class SilentEgressPacketLossParams(BaseModel):
    host_name: str = Field(
        description="Gateway or spine that owns the egress interface."
    )
    intf_name: str = Field(description="Target egress interface.")
    bmv2_port: int = Field(gt=0)
    loss_basis_points: int = Field(default=200, ge=1, le=10000)
    seed: int = 42


class SilentEgressPacketLoss(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name = "silent_egress_packet_loss"
    symptom_desc = (
        "A deterministic packet subset disappears on one healthy egress interface."
    )
    TAGS = ["p4_runtime", "telemetry", "flow_tracking"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    Params = SilentEgressPacketLossParams

    def root_cause_resources(self, params: SilentEgressPacketLossParams):
        return [interface_resource(params.host_name, params.intf_name)]

    @staticmethod
    def register_threshold(basis_points: int) -> int:
        return round(65535 * basis_points / 10000)

    def inject_fault(self, params: SilentEgressPacketLossParams):
        threshold = self.register_threshold(params.loss_basis_points)
        self._result = set_deterministic_loss(
            self.runtime, params.host_name, params.bmv2_port, threshold
        )
        self.runtime.exec(
            params.host_name,
            f"printf '%s\\n' '{self._result}' > /tmp/nika_silent_egress_result",
        )

    def verify_fault(self, params: SilentEgressPacketLossParams) -> dict:
        output = getattr(self, "_result", "") or self.runtime.exec(
            params.host_name,
            "cat /tmp/nika_silent_egress_result 2>/dev/null || true",
        )
        threshold = self.register_threshold(params.loss_basis_points)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=str(threshold) in output,
            details={
                "interface": params.intf_name,
                "loss_basis_points": params.loss_basis_points,
                "seed": params.seed,
                "output": output[:200],
            },
        )
