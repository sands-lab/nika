import ipaddress
import re
import time

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger


def _client_subnet(runtime, client_host: str, dhcp_server: str | None = None) -> str:
    """Derive the client's IPv4 network address for dhcpd subnet matching."""
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
        subnet = params.subnet or _client_subnet(self.runtime, client_host, dhcp_server)
        self.runtime.dhcp_delete_subnet(dhcp_server, subnet)
        time.sleep(1.0)
        self._injected_subnet = subnet

    def _subnet_stanza_count(self, dhcp_server: str, subnet: str) -> int:
        escaped = subnet.replace(".", "\\.")
        raw = self.runtime.exec(
            dhcp_server,
            f"grep -E 'subnet {escaped} netmask' /etc/dhcp/dhcpd.conf 2>/dev/null | wc -l",
        ).strip()
        try:
            return int(raw)
        except ValueError:
            return -1

    def verify_fault(self, params: DHCPMissingSubnetParams) -> dict:
        """Verify the deleted subnet is absent from dhcpd.conf."""
        dhcp_server = params.host_name
        client_host = params.host_name_2
        subnet = (
            params.subnet
            or getattr(self, "_injected_subnet", None)
            or _client_subnet(self.runtime, client_host, dhcp_server)
        )
        match_count = self._subnet_stanza_count(dhcp_server, subnet)
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
