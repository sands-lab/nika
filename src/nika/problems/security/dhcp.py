from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import dhcp_server_client
from nika.problems.support.inject_resolve import unused_address_near
from nika.problems.support.dhcp import (
    client_subnet,
    set_subnet_option,
    subnet_declared,
    subnet_option_value,
)


# ==================================================================
# Problem: DHCP distributing spoofed gateway to hosts
# ==================================================================


class DHCPSpoofedGatewayParams(BaseModel):
    """Parameters for injecting a DHCP spoofed gateway fault."""

    host_name: str = Field(description="DHCP server host name.")
    host_name_2: str = Field(description="Affected client host name.")


class DHCPSpoofedGateway(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "dhcp_spoofed_gateway"

    description = (
        "The DHCP server advertises an incorrect default gateway (router "
        "option) to the clients of a subnet."
    )
    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedGatewayParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return dhcp_server_client(ctx)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: DHCPSpoofedGatewayParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPSpoofedGatewayParams):
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = client_subnet(self.runtime, client_host, dhcp_server)
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
    wrong_dns: str | None = Field(
        default=None,
        description=(
            "Spoofed DNS IP. Defaults to an unused, non-resolving address next "
            "to the subnet's real resolver."
        ),
    )


class DHCPSpoofedDNS(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "dhcp_spoofed_dns"

    description = "DHCP distributes a spoofed DNS server option."
    symptom_desc = "Some hosts can not access webservices."
    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedDNSParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return dhcp_server_client(ctx)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: DHCPSpoofedDNSParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPSpoofedDNSParams):
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = client_subnet(self.runtime, client_host, dhcp_server)
        self._wrong_dns = self._spoofed_dns(params, subnet)
        self.runtime.dhcp_set_option_dns(dhcp_server, subnet, self._wrong_dns)
        self.runtime.renew_dhcp_leases(self.runtime.list_dhcp_client_nodes())

    def _spoofed_dns(self, params: DHCPSpoofedDNSParams, subnet: str) -> str:
        if params.wrong_dns:
            return params.wrong_dns
        current = subnet_option_value(
            self.runtime, params.host_name, subnet, "domain-name-servers"
        )
        real = (current or "").split(",")[0].strip() or subnet
        return unused_address_near(self.runtime, real)

    def verify_fault(self, params: DHCPSpoofedDNSParams) -> dict:
        """Verify dhcpd.conf has the spoofed DNS server option."""
        dhcp_server = params.host_name
        wrong_dns = params.wrong_dns or getattr(self, "_wrong_dns", None)
        grep_result = self.runtime.exec(
            dhcp_server,
            f"grep 'option domain-name-servers.*{wrong_dns}' /etc/dhcp/dhcpd.conf && echo found || echo absent",
        ).strip()
        verified = bool(wrong_dns) and "found" in grep_result
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "dhcp_server": dhcp_server,
                "wrong_dns": wrong_dns,
                "grep_result": grep_result,
            },
        )


# ==================================================================
# Problem: DHCP distributing a spoofed subnet mask to hosts
# ==================================================================


class DHCPSpoofedSubnetParams(BaseModel):
    """Parameters for injecting a DHCP spoofed subnet fault."""

    host_name: str = Field(description="DHCP server host name.")
    host_name_2: str = Field(description="Affected client host name.")
    subnet: str | None = Field(
        default=None,
        description="IPv4 network address of the target subnet declaration; derived from the client when omitted.",
    )
    wrong_netmask: str = Field(
        default="255.255.255.252",
        description="Spoofed subnet mask handed to clients (the declaration stays).",
    )


class DHCPSpoofedSubnet(ProblemBase):
    """The legitimate server hands out an attacker-chosen subnet mask.

    Unlike ``dhcp_missing_subnet`` the subnet declaration stays and clients
    still get leases, but with a prefix that no longer covers their gateway.
    """

    failure_domain = FailureDomain.SECURITY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "dhcp_spoofed_subnet"

    description = "DHCP distributes a spoofed subnet mask to clients."
    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedSubnetParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = dhcp_server_client(ctx)
        if ctx.scenario == "campus_lan":
            params["subnet"] = "10.1.1.0"
        return params

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: DHCPSpoofedSubnetParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPSpoofedSubnetParams):
        dhcp_server = params.host_name
        subnet = params.subnet or client_subnet(
            self.runtime, params.host_name_2, dhcp_server
        )
        self._subnet = subnet
        set_subnet_option(
            self.runtime, dhcp_server, subnet, "subnet-mask", params.wrong_netmask
        )
        self.runtime.renew_dhcp_leases(self.runtime.list_dhcp_client_nodes())

    def verify_fault(self, params: DHCPSpoofedSubnetParams) -> dict:
        """Verify the subnet declaration remains and serves the spoofed mask."""
        dhcp_server = params.host_name
        subnet = (
            params.subnet
            or getattr(self, "_subnet", None)
            or client_subnet(self.runtime, params.host_name_2, dhcp_server)
        )
        declarations = subnet_declared(self.runtime, dhcp_server, subnet)
        mask = subnet_option_value(self.runtime, dhcp_server, subnet, "subnet-mask")
        verified = declarations == 1 and mask == params.wrong_netmask
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "dhcp_server": dhcp_server,
                "subnet": subnet,
                "declarations": declarations,
                "subnet_mask_option": mask,
            },
        )
