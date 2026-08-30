from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)
from nika.problems.root_cause import node_resource

# ==========================================
# Problem: Host crash simulated by pausing a docker instance
# ==========================================


class HostCrashParams(BaseModel):
    """Parameters for injecting a host-crash fault."""

    host_name: str = Field(description="Target host name.")


class HostCrash(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.END_HOST_FAILURE
    root_cause_name: str = "host_crash"
    TAGS: str = ["pc"]

    Params = HostCrashParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: HostCrashParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HostCrashParams):
        self.runtime.pause(params.host_name)

    def verify_fault(self, params: HostCrashParams) -> dict:
        """Verify the host container is paused (simulated crash)."""
        container_status = self.runtime.node_status(params.host_name)
        verified = container_status == "paused"
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={"host": params.host_name, "container_status": container_status},
        )
