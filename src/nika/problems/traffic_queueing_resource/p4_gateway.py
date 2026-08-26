"""Queue and traffic-induced failures for the P4 gateway benchmark."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.problem_base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.root_cause import interface_resource
from nika.problems.support.p4_gateway import set_ecn_threshold


class P4EcnThresholdMisconfigurationParams(BaseModel):
    host_name: str
    intf_name: str
    bmv2_port: int = Field(gt=0)
    threshold: int = Field(default=1024, gt=0)


class P4EcnThresholdMisconfiguration(ProblemBase):
    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    root_cause_name = "p4_ecn_threshold_misconfiguration"
    symptom_desc = (
        "TCP incast builds a deeper queue because CE marking starts too late."
    )
    TAGS = ["p4_runtime", "ecn", "queue", "http"]
    Params = P4EcnThresholdMisconfigurationParams

    def root_cause_resources(self, params: P4EcnThresholdMisconfigurationParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def inject_fault(self, params: P4EcnThresholdMisconfigurationParams):
        self._result = set_ecn_threshold(
            self.runtime, params.host_name, params.bmv2_port, params.threshold
        )

    def verify_fault(self, params: P4EcnThresholdMisconfigurationParams) -> dict:
        output = getattr(self, "_result", "")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=str(params.threshold) in output,
            details={"interface": params.intf_name, "threshold": params.threshold},
        )
