"""Software-switch forwarding failure implementations."""

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource

from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)


class Bmv2SwitchDownParams(BaseModel):
    """Parameters for injecting a BMv2 switch down fault."""

    host_name: str = Field(description="Target BMv2 switch name.")


class Bmv2SwitchDown(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "bmv2_switch_down"
    TAGS: str = ["p4"]

    Params = Bmv2SwitchDownParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: Bmv2SwitchDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: Bmv2SwitchDownParams):
        self.runtime.exec(params.host_name, "pkill simple_switch")

    def verify_fault(self, params: Bmv2SwitchDownParams) -> dict:
        """Verify simple_switch process is NOT running on the BMv2 switch."""
        pgrep_output = self.runtime.exec(
            params.host_name, "pgrep -a simple_switch 2>/dev/null || echo NONE"
        ).strip()
        verified = pgrep_output == "NONE" or "simple_switch" not in pgrep_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )
