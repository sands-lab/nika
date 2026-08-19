from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.root_cause import node_resource
from nika.utils.logger import system_logger

logger = system_logger

# ==================================================================
# Problem: DNS service down
# ==================================================================


class DNSServiceDownParams(BaseModel):
    """Parameters for injecting a DNS service down fault."""

    host_name: str = Field(description="Target DNS server host name.")
    service_name: str = Field(default="named", description="Service name.")


class DNSServiceDown(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "dns_service_down"
    symptom_desc = "Some hosts cannot access external websites."
    TAGS: str = ["dns"]

    Params = DNSServiceDownParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.service_name = "named"

    def root_cause_resources(self, params: DNSServiceDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DNSServiceDownParams):
        self.runtime.kill_process(params.host_name, "named")

    def verify_fault(self, params: DNSServiceDownParams) -> dict:
        """Verify named process is not running."""
        verified = self.runtime.process_not_running(params.host_name, "named")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "service": params.service_name},
        )


# ==================================================================
# Problem: DHCP service down
# ==================================================================


class DHCPServiceDownParams(BaseModel):
    """Parameters for injecting a DHCP service down fault."""

    host_name: str = Field(description="Target DHCP server host name.")
    service_name: str = Field(default="isc-dhcp-server", description="Service name.")


class DHCPServiceDown(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "dhcp_service_down"

    TAGS: str = ["dhcp"]

    Params = DHCPServiceDownParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.service_name = "isc-dhcp-server"

    def root_cause_resources(self, params: DHCPServiceDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPServiceDownParams):
        self.runtime.kill_process(params.host_name, "dhcpd")

    def verify_fault(self, params: DHCPServiceDownParams) -> dict:
        """Verify DHCP server process is not running."""
        verified = self.runtime.process_not_running(params.host_name, "dhcpd")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "service": params.service_name},
        )
