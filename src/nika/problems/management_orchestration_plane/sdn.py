from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    ProblemBase,
    build_verify_result,
)
from nika.utils.logger import system_logger

logger = system_logger

# ==================================================================
# Problem: SDN controller crash
# ==================================================================


class SDNControllerCrashParams(BaseModel):
    """Parameters for injecting an SDN controller crash fault."""

    host_name: str = Field(description="Target SDN controller host name.")


class SDNControllerCrash(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    cause = FailureCause.SOFTWARE
    symptom = FailureSymptom.DOWN
    scope = FailureScope.SERVICE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "sdn_controller_crash"
    TAGS: str = ["sdn"]

    Params = SDNControllerCrashParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SDNControllerCrashParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SDNControllerCrashParams):
        self.runtime.exec(params.host_name, "pkill -f pox.py")

    def verify_fault(self, params: SDNControllerCrashParams) -> dict:
        """Verify POX controller is NOT running on the SDN controller."""
        pgrep_output = self.runtime.exec(
            params.host_name,
            "pgrep -af pox 2>/dev/null | grep -v 'pgrep\\|bash\\|grep' | grep . || echo NONE",
        ).strip()
        verified = pgrep_output == "NONE" or "pox" not in pgrep_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )


# ==================================================================
# Problem: Southbound port block
# ==================================================================


class SouthboundPortBlockParams(BaseModel):
    """Parameters for injecting a southbound port block fault."""

    host_name: str = Field(description="Target SDN controller host name.")
    southbound_port: int = Field(default=6633, description="Port to block.")


class SouthboundPortBlock(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.DOWN
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "southbound_port_block"
    TAGS: str = ["sdn"]

    Params = SouthboundPortBlockParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SouthboundPortBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SouthboundPortBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, f"tcp dport {params.southbound_port} drop"
        )

    def verify_fault(self, params: SouthboundPortBlockParams) -> dict:
        """Verify nftables has a rule blocking the southbound port."""
        nft_output = self.runtime.exec(
            params.host_name, "nft list ruleset 2>/dev/null"
        ).strip()
        verified = (
            f"tcp dport {params.southbound_port}" in nft_output and "drop" in nft_output
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "nft_output": nft_output},
        )


# ==================================================================
# Problem: Southbound port mismatch
# ==================================================================


class SouthboundPortMismatchParams(BaseModel):
    """Parameters for injecting a southbound port mismatch fault."""

    host_name: str = Field(description="Target SDN controller host name.")
    mismatched_port: int = Field(default=6653, description="Port used after restart.")
    original_port: int = Field(
        default=6633, description="Expected original OpenFlow port."
    )


class SouthboundPortMismatch(ProblemBase):
    failure_domain = FailureDomain.MANAGEMENT_ORCHESTRATION_PLANE
    cause = FailureCause.CONFIGURATION
    symptom = FailureSymptom.DOWN
    scope = FailureScope.PATH
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.COMPLETE
    root_cause_name: str = "southbound_port_mismatch"
    TAGS: str = ["sdn"]

    Params = SouthboundPortMismatchParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: SouthboundPortMismatchParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: SouthboundPortMismatchParams):
        self.runtime.exec(params.host_name, "pkill -f pox.py")
        self.runtime.exec(
            params.host_name,
            f"python3 /pox/pox.py openflow.of_01 --port={params.mismatched_port} forwarding.l2_learning &",
        )

    def verify_fault(self, params: SouthboundPortMismatchParams) -> dict:
        """Verify POX controller is running with the mismatched port."""
        pgrep_output = self.runtime.exec(
            params.host_name,
            "pgrep -af pox 2>/dev/null | grep -v 'pgrep\\|bash\\|grep' | grep . || echo NONE",
        ).strip()
        running = "pox" in pgrep_output and pgrep_output != "NONE"
        has_port = str(params.mismatched_port) in pgrep_output
        verified = running and has_port
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )


# ==================================================================
# Problem: Flow rule shadowing
# ==================================================================


# ==================================================================
# Problem: Flow rule loop
# ==================================================================
