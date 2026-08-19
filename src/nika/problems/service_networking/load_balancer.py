from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)

# ==================================================================
# Problem: Load balancer overload causing performance degradation.
# ==================================================================


class LoadBalancerOverloadParams(BaseModel):
    """Parameters for injecting a load balancer overload fault."""

    host_name: str = Field(description="Target load balancer host name.")
    duration: int = Field(default=300, description="Stress duration in seconds.")


class LoadBalancerOverload(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    root_cause_name: str = "load_balancer_overload"
    TAGS: str = ["load_balancer", "http"]

    Params = LoadBalancerOverloadParams

    def __init__(self, scenario_name: str = "load_balancer", **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: LoadBalancerOverloadParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: LoadBalancerOverloadParams):
        self.runtime.exec(
            params.host_name,
            f"nohup stress-ng --cpu 0 --cpu-load 100 --iomix 0 --sock 0 --hdd 2 --vm 0 --vm-bytes 75% --timeout {params.duration} </dev/null >/dev/null 2>&1 &",
        )

    def verify_fault(self, params: LoadBalancerOverloadParams) -> dict:
        """Verify stress-ng is running on the load balancer."""
        pgrep_output = self.runtime.exec(
            params.host_name, "pgrep -a stress-ng 2>/dev/null || echo NONE"
        ).strip()
        verified = "stress-ng" in pgrep_output and pgrep_output != "NONE"
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )
