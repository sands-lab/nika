import time

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.dhcp import client_subnet, subnet_declared
from nika.problems.support.benchmark_targets import (
    dhcp_server_client,
)
from nika.utils.logger import system_logger


# ==================================================================
# Problem: DHCP missing subnet
# ==================================================================


class DHCPMissingSubnetParams(BaseModel):
    """Parameters for injecting a DHCP missing subnet fault."""

    host_name: str = Field(description="DHCP server host name.")
    host_name_2: str = Field(description="Affected client host name.")
    subnet: str | None = Field(
        default=None,
        description="IPv4 network address to remove; derived from the client when omitted.",
    )


class DHCPMissingSubnet(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    root_cause_name: str = "dhcp_missing_subnet"

    description = "DHCP server is missing a subnet configuration."
    TAGS: str = ["dhcp"]

    Params = DHCPMissingSubnetParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = dhcp_server_client(ctx)
        if ctx.scenario == "campus_lan":
            params["subnet"] = "10.1.1.0"
        return params

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: DHCPMissingSubnetParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPMissingSubnetParams):
        dhcp_server = params.host_name
        client_host = params.host_name_2
        system_logger.info(
            f"Injecting DHCP missing subnet fault: DHCP server {dhcp_server}, affected host {client_host}"
        )
        subnet = params.subnet or client_subnet(self.runtime, client_host, dhcp_server)
        self.runtime.dhcp_delete_subnet(dhcp_server, subnet)
        time.sleep(1.0)
        self._injected_subnet = subnet
        # Renew now so clients do not keep a valid lease until it expires.
        self.runtime.renew_dhcp_leases(self.runtime.list_dhcp_client_nodes())

    def verify_fault(self, params: DHCPMissingSubnetParams) -> dict:
        """Verify the deleted subnet is absent from dhcpd.conf."""
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = (
            params.subnet
            or getattr(self, "_injected_subnet", None)
            or client_subnet(self.runtime, client_host, dhcp_server)
        )
        match_count = subnet_declared(self.runtime, dhcp_server, subnet)
        verified = match_count == 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "dhcp_server": dhcp_server,
                "subnet": subnet,
                "match_count": match_count,
            },
        )
