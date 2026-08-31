import ipaddress
import re

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)


def _resolve_client_subnet(
    runtime, client_host: str, dhcp_server: str | None = None
) -> str:
    """Derive client IPv4 network address for dhcpd option targeting."""
    ip = runtime.get_host_ip(client_host, with_prefix=True)
    if not ip:
        for intf in ("eth0", "eth1"):
            line = runtime.exec(
                client_host,
                f"ip -4 -o addr show dev {intf} scope global 2>/dev/null | head -1",
            ).strip()
            match = re.search(r"inet\s+(\S+)", line)
            if match:
                ip = match.group(1)
                break
    if not ip and dhcp_server:
        conf = runtime.exec(
            dhcp_server,
            "awk '/^subnet /{print $2; exit}' /etc/dhcp/dhcpd.conf 2>/dev/null || true",
        ).strip()
        if conf:
            return str(ipaddress.ip_address(conf))
    if not ip:
        raise ValueError(f"No IPv4 address on DHCP client {client_host}")
    return str(ipaddress.ip_network(ip, strict=False).network_address)


# ==================================================================
# Problem: DHCP distributing spoofed gateway to hosts
# ==================================================================


class DHCPSpoofedGatewayParams(BaseModel):
    """Parameters for injecting a DHCP spoofed gateway fault."""

    host_name: str = Field(description="DHCP server host name.")
    host_name_2: str = Field(description="Affected client host name.")


class DHCPSpoofedGateway(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "dhcp_spoofed_gateway"

    description = "DHCP distributes a spoofed default gateway."
    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedGatewayParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def _client_subnet(self, client_host: str) -> str:
        return _resolve_client_subnet(self.runtime, client_host, "dhcp_server")

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
    wrong_dns: str = Field(
        default="192.0.2.1",
        description="Spoofed DNS IP (TEST-NET; non-resolving).",
    )


class DHCPSpoofedDNS(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "dhcp_spoofed_dns"

    description = "DHCP distributes a spoofed DNS server option."
    symptom_desc = "Some hosts can not access webservices."
    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedDNSParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def _client_subnet(self, client_host: str) -> str:
        return _resolve_client_subnet(self.runtime, client_host, "dhcp_server")

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
    subnet: str | None = Field(
        default=None,
        description="IPv4 network address to remove; derived from the client when omitted.",
    )


class DHCPSpoofedSubnet(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "dhcp_spoofed_subnet"

    description = "DHCP subnet configuration is spoofed or removed for clients."
    TAGS: str = ["dhcp"]

    Params = DHCPSpoofedSubnetParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def _client_subnet(self, client_host: str) -> str:
        return _resolve_client_subnet(self.runtime, client_host, "dhcp_server")

    def root_cause_resources(self, params: DHCPSpoofedSubnetParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPSpoofedSubnetParams):
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = params.subnet or self._client_subnet(client_host)
        self.deleted_subnet = subnet
        self.runtime.dhcp_delete_subnet(dhcp_server, subnet)

    def verify_fault(self, params: DHCPSpoofedSubnetParams) -> dict:
        """Verify the target subnet has been removed from dhcpd.conf."""
        dhcp_server = params.host_name
        subnet = (
            params.subnet
            or getattr(self, "deleted_subnet", None)
            or self._client_subnet(params.host_name_2)
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
