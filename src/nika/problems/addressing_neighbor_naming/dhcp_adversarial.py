import ipaddress

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    build_verify_result,
    ProblemBase,
)

# ==================================================================
# Problem: DHCP distributing spoofed gateway to hosts
# ==================================================================


class DHCPSpoofedGatewayParams(BaseModel):
    """Parameters for injecting a DHCP spoofed gateway fault."""

    host_name: str = Field(description="DHCP server host name.")
    host_name_2: str = Field(description="Affected client host name.")


class DHCPSpoofedGateway(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    cause = FailureCause.ADVERSARIAL
    symptom = FailureSymptom.MISROUTING
    scope = FailureScope.MULTI_NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "dhcp_spoofed_gateway"

    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedGatewayParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def _client_subnet(self, client_host: str) -> str:
        return str(
            ipaddress.ip_network(
                self.runtime.get_host_ip(client_host, with_prefix=True), strict=False
            ).network_address
        )

    def root_cause_resources(self, params: DHCPSpoofedGatewayParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPSpoofedGatewayParams):
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = self._client_subnet(client_host)
        wrong_gw = ".".join(subnet.split(".")[:3] + ["254"])
        self.runtime.dhcp_set_option_routers(dhcp_server, subnet, wrong_gw)
        self.runtime.renew_dhcp_leases(self.runtime.list_dhcp_client_nodes())

    def verify_fault(self, params: DHCPSpoofedGatewayParams) -> dict:
        """Verify dhcpd.conf has spoofed gateway ending in .254."""
        dhcp_server = params.host_name
        grep_result = self.runtime.exec(
            dhcp_server,
            "grep 'option routers.*\\.254' /etc/dhcp/dhcpd.conf && echo found || echo absent",
        ).strip()
        verified = "found" in grep_result
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"dhcp_server": dhcp_server, "grep_result": grep_result},
        )


# ==================================================================
# Problem: DHCP distributing spoofed DNS to hosts
# ==================================================================


class DHCPSpoofedDNSParams(BaseModel):
    """Parameters for injecting a DHCP spoofed DNS fault."""

    host_name: str = Field(description="DHCP server host name.")
    host_name_2: str = Field(description="Affected client host name.")
    wrong_dns: str = Field(default="8.8.8.8", description="Spoofed DNS IP.")


class DHCPSpoofedDNS(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    cause = FailureCause.ADVERSARIAL
    symptom = FailureSymptom.MISROUTING
    scope = FailureScope.MULTI_NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "dhcp_spoofed_dns"

    symptom_desc = "Some hosts can not access webservices."
    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedDNSParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def _client_subnet(self, client_host: str) -> str:
        return str(
            ipaddress.ip_network(
                self.runtime.get_host_ip(client_host, with_prefix=True), strict=False
            ).network_address
        )

    def root_cause_resources(self, params: DHCPSpoofedDNSParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPSpoofedDNSParams):
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = self._client_subnet(client_host)
        self.runtime.dhcp_set_option_dns(dhcp_server, subnet, params.wrong_dns)
        self.runtime.renew_dhcp_leases(self.runtime.list_dhcp_client_nodes())

    def verify_fault(self, params: DHCPSpoofedDNSParams) -> dict:
        """Verify dhcpd.conf has spoofed DNS server 8.8.8.8."""
        dhcp_server = params.host_name
        grep_result = self.runtime.exec(
            dhcp_server,
            f"grep 'option domain-name-servers.*{params.wrong_dns}' /etc/dhcp/dhcpd.conf && echo found || echo absent",
        ).strip()
        verified = "found" in grep_result
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "dhcp_server": dhcp_server,
                "wrong_dns": params.wrong_dns,
                "grep_result": grep_result,
            },
        )


# ==================================================================
""" Problem: DHCP missing subnet configuration """
# ==================================================================


class DHCPSpoofedSubnetParams(BaseModel):
    """Parameters for injecting a DHCP spoofed subnet fault."""

    host_name: str = Field(description="DHCP server host name.")
    host_name_2: str = Field(description="Affected client host name.")


class DHCPSpoofedSubnet(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    cause = FailureCause.ADVERSARIAL
    symptom = FailureSymptom.MISROUTING
    scope = FailureScope.MULTI_NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "dhcp_spoofed_subnet"

    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedSubnetParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def _client_subnet(self, client_host: str) -> str:
        return str(
            ipaddress.ip_network(
                self.runtime.get_host_ip(client_host, with_prefix=True), strict=False
            ).network_address
        )

    def root_cause_resources(self, params: DHCPSpoofedSubnetParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPSpoofedSubnetParams):
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = self._client_subnet(client_host)
        self.deleted_subnet = subnet
        self.runtime.dhcp_delete_subnet(dhcp_server, subnet)
        self.runtime.renew_dhcp_leases(self.runtime.list_dhcp_client_nodes())

    def verify_fault(self, params: DHCPSpoofedSubnetParams) -> dict:
        """Verify the target subnet has been removed from dhcpd.conf."""
        dhcp_server = params.host_name
        subnet = getattr(self, "deleted_subnet", None) or self._client_subnet(
            params.host_name_2
        )
        sub_escaped = subnet.replace(".", "\\.")
        match_output = self.runtime.exec(
            dhcp_server,
            f"grep 'subnet {sub_escaped} netmask' /etc/dhcp/dhcpd.conf | wc -l",
        ).strip()
        count_output = self.runtime.exec(
            dhcp_server,
            "grep 'subnet.*netmask' /etc/dhcp/dhcpd.conf | wc -l",
        ).strip()
        try:
            match_count = int(match_output)
        except ValueError:
            match_count = -1
        try:
            subnet_count = int(count_output)
        except ValueError:
            subnet_count = -1
        verified = match_count == 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "dhcp_server": dhcp_server,
                "subnet_count": subnet_count,
                "deleted_subnet": subnet,
            },
        )
