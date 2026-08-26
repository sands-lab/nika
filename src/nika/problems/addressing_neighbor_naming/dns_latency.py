from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca.inventory import interface_on


class DNSLookupLatencyParams(BaseModel):
    """Parameters for injecting a DNS lookup latency fault."""

    host_name: str = Field(description="Target DNS server host name.")
    intf_name: str = Field(default="eth0", description="Interface name.")
    delay_ms: int = Field(default=1000, description="Delay in milliseconds.")


class DNSLookupLatency(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "dns_lookup_latency"
    symptom_desc: str = "Users experience high latency when accessing web services."
    TAGS: str = ["dns", "http"]

    Params = DNSLookupLatencyParams

    def __init__(self, scenario_name: str = "dc_clos", **kwargs):
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
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "intf": params.intf_name,
                "tc_output": tc_output,
            },
        )
