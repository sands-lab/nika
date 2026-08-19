from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

logger = system_logger

# ==================================================================
# Problem: P4 aggressive detection thresholds of Bloom filter
# ==================================================================


class P4AggressiveDetectionThresholdsParams(BaseModel):
    """Parameters for injecting a P4 aggressive detection thresholds fault."""

    host_name: str = Field(description="Target BMv2 switch name.")
    p4_name: Optional[str] = Field(
        default=None,
        description="P4 program name (without suffix). Defaults to runtime detection.",
    )


class P4AggressiveDetectionThresholds(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.LOSS
    scope = FailureScope.NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name = "p4_aggressive_detection_thresholds"
    TAGS: str = ["p4", "bloom_filter"]

    Params = P4AggressiveDetectionThresholdsParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: P4AggressiveDetectionThresholdsParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4AggressiveDetectionThresholdsParams):
        p4_name = (
            params.p4_name
            if params.p4_name is not None
            else getattr(self, "p4_name", None)
        )
        if p4_name is None:
            p4_name = self.runtime.exec(
                params.host_name, "echo *.p4 | sed 's/\\.p4//'"
            ).strip()
        self.runtime.exec(
            params.host_name,
            f"cp {p4_name}.p4 {p4_name}.p4.bak && "
            f"rm {p4_name}.json && "
            f"sed -Ei 's/#define PACKET_THRESHOLD 1000/#define PACKET_THRESHOLD 100/g' {p4_name}.p4 ",
        )
        self.runtime.exec(params.host_name, "pkill -f simple_switch")
        self.runtime.exec(params.host_name, f"./hostlab/{params.host_name}.startup")

    def verify_fault(self, params: P4AggressiveDetectionThresholdsParams) -> dict:
        """Verify PACKET_THRESHOLD was changed to 100 in the P4 source."""
        p4_name = (
            params.p4_name
            if params.p4_name is not None
            else getattr(self, "p4_name", None)
        )
        if p4_name is None:
            p4_name = self.runtime.exec(
                params.host_name, "echo *.p4 | sed 's/\\.p4//'"
            ).strip()
        threshold_check = self.runtime.exec(
            params.host_name,
            f"grep 'PACKET_THRESHOLD 100' {p4_name}.p4 2>/dev/null && echo found || echo absent",
        ).strip()
        json_check = self.runtime.exec(
            params.host_name,
            f"ls {p4_name}.json 2>/dev/null && echo exists || echo missing",
        ).strip()
        threshold_modified = "found" in threshold_check
        json_exists = "exists" in json_check
        verified = threshold_modified
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "threshold_modified": threshold_modified,
                "json_exists": json_exists,
            },
        )
