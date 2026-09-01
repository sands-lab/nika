"""Software-switch forwarding failure implementations."""

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource

from nika.problems.base import (
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
    description = "BMv2 switch dataplane process is down."
    TAGS: str = ["p4"]

    Params = Bmv2SwitchDownParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: Bmv2SwitchDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: Bmv2SwitchDownParams):
        # -f: match full cmdline (comm is truncated to 15 chars on Linux).
        self.runtime.exec(
            params.host_name,
            "pkill -9 -f '[s]imple_switch' 2>/dev/null || true",
        )

    def verify_fault(self, params: Bmv2SwitchDownParams) -> dict:
        """Verify the BMv2 dataplane process is not running (artifact gate)."""
        # Exclude zombies: pkill -9 can leave a <defunct> entry that still
        # matches pgrep -af '[s]imple_switch'.
        pgrep_output = self.runtime.exec(
            params.host_name,
            "pgrep -af '[s]imple_switch' 2>/dev/null "
            "| grep -v '<defunct>' || echo NONE",
        ).strip()
        verified = pgrep_output == "NONE" or "simple_switch" not in pgrep_output
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )
