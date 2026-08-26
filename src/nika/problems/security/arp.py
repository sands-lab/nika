from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

# ==================================================================
# Problem: Arp cache poisoning causing data plane issues.
# ==================================================================


class ArpCachePoisoningParams(BaseModel):
    """Parameters for injecting an ARP cache poisoning fault."""

    host_name: str = Field(description="Target host name.")
    fake_mac: str = Field(
        default="00:11:22:33:44:55", description="Forged MAC address."
    )


class ArpCachePoisoning(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "arp_cache_poisoning"
    TAGS: str = ["arp"]

    Params = ArpCachePoisoningParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: ArpCachePoisoningParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: ArpCachePoisoningParams):
        default_gateway = self.runtime.get_default_gateway(params.host_name)
        self.runtime.exec(
            params.host_name, f"arp -s {default_gateway} {params.fake_mac}"
        )

    def verify_fault(self, params: ArpCachePoisoningParams) -> dict:
        """Verify the ARP cache has the fake MAC for the default gateway."""
        gateway = self.runtime.get_default_gateway(params.host_name)
        neigh_output = self.runtime.exec(
            params.host_name, f"ip neigh show | grep '{params.fake_mac}'"
        ).strip()
        verified = bool(neigh_output)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "gateway": gateway,
                "fake_mac": params.fake_mac,
                "neigh_entry": neigh_output,
            },
        )
