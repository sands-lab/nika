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
from nika.problems.support.dhcp import (
    DHCPD_CONF,
    client_subnet,
    dhcp_client_step,
    host_ipv4,
    lease_option,
    subnet_declared,
)
from nika.problems.support.benchmark_targets import (
    dhcp_server_client,
)
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
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
    symptom_probe = "custom"
    symptom_class = "unreachable"
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

    def oracle_playbook(self, params: DHCPMissingSubnetParams) -> OraclePlaybook:
        server, client = params.host_name, params.host_name_2

        def lease_subnet(out) -> str:
            if params.subnet:
                return params.subnet
            text = out.get("lease", "")
            m = re.search(r"fixed-address ([\d.]+);", text)
            mask = lease_option(text, "subnet-mask")
            if not (m and mask):
                return ""
            addr = m.group(1)
            return str(
                ipaddress.ip_network(f"{addr}/{mask}", strict=False).network_address
            )

        def no_declaration(out):
            subnet = lease_subnet(out)
            if not subnet:
                # Without the segment's subnet the check cannot hold.
                return {
                    "host_name": server,
                    "command": "echo SUBNET_UNKNOWN",
                    "timeout": 15,
                }
            subnet = re.escape(subnet)
            return {
                "host_name": server,
                "command": (
                    "pgrep -af dhcpd | grep -v pgrep || echo NO_DHCPD; "
                    f"grep -nE '^[[:space:]]*subnet ' {DHCPD_CONF}; "
                    f"grep -cE '^[[:space:]]*subnet {subnet} netmask ' {DHCPD_CONF} "
                    "| sed 's/^/client_subnet_declarations=/'"
                ),
                "timeout": 15,
            }

        return OraclePlaybook(
            finding=(
                f"The DHCP server {server} has no subnet declaration for the "
                f"segment of {client}, so its clients get no lease or address."
            ),
            steps=[
                dhcp_client_step(
                    client,
                    "ip -br link show dev eth0; ip -4 -o addr show dev eth0 scope global; "
                    "tail -n 6 /var/log/startup.log 2>/dev/null",
                    f"Check whether DHCP client {client} holds an address and lease.",
                    all_of(
                        contains(r"^eth0(@\S+)?\s+UP\b", "eth0 is up"),
                        Check(
                            "eth0 has no IPv4 address",
                            lambda o: host_ipv4(o.split("dhclient=")[0]) is None,
                        ),
                        contains(r"^dhclient=running", "dhclient is still requesting"),
                    ),
                    evidence=True,
                    save_as="lease",
                    attempts=3,
                ),
                OracleStep(
                    f"Check that dhcpd on {server} runs but declares no subnet for "
                    f"the client segment.",
                    "exec_shell",
                    no_declaration,
                    all_of(
                        lacks(r"^NO_DHCPD$", "dhcpd is running"),
                        contains(r"^\d+:\s*subnet ", "other subnets are declared"),
                        contains(
                            r"^client_subnet_declarations=0$",
                            "the client's subnet is not declared",
                        ),
                    ),
                    evidence=True,
                ),
            ],
        )
