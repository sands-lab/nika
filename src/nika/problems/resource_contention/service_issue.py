from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.topology_inventory import interface_on
from nika.problems.problem_base import (
    RootCauseCategory,
    build_verify_result,
    ProblemBase,
)

# ==================================================================
# Problem: Web service experiencing high DNS lookup latency causing performance degradation.
# ==================================================================


class DNSLookupLatencyParams(BaseModel):
    """Parameters for injecting a DNS lookup latency fault."""

    host_name: str = Field(description="Target DNS server host name.")
    intf_name: str = Field(default="eth0", description="Interface name.")
    delay_ms: int = Field(default=1000, description="Delay in milliseconds.")


class DNSLookupLatency(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.RESOURCE_CONTENTION
    root_cause_name: str = "dns_lookup_latency"
    symptom_desc: str = "Users experience high latency when accessing web services."
    TAGS: str = ["dns", "http"]

    Params = DNSLookupLatencyParams

    def __init__(self, scenario_name: str = "dc_clos_service", **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: DNSLookupLatencyParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: DNSLookupLatencyParams):
        self.runtime.tc_set_netem(
            params.host_name, params.intf_name, delay_ms=params.delay_ms
        )

    def verify_fault(self, params: DNSLookupLatencyParams) -> dict:
        """Verify tc qdisc on DNS server interface has a delay configured."""
        tc_output = self.runtime.exec(
            params.host_name, f"tc qdisc show dev {params.intf_name}"
        ).strip()
        verified = "delay" in tc_output
        return build_verify_result(
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={
                "host": params.host_name,
                "intf": params.intf_name,
                "tc_output": tc_output,
            },
        )


# ==================================================================
# Problem: Load balancer overload causing performance degradation.
# ==================================================================


class LoadBalancerOverloadParams(BaseModel):
    """Parameters for injecting a load balancer overload fault."""

    host_name: str = Field(description="Target load balancer host name.")
    duration: int = Field(default=300, description="Stress duration in seconds.")


class LoadBalancerOverload(ProblemBase):
    root_cause_category: RootCauseCategory = RootCauseCategory.RESOURCE_CONTENTION
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
            root_cause_name=self.root_cause_name,
            faulty_devices=self.faulty_devices,
            verified=verified,
            details={"host": params.host_name, "pgrep_output": pgrep_output},
        )
