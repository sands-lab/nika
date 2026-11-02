from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca import node_resource
from nika.problems.support.dns import dns_client_steps
from nika.problems.support.oracle import (
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
    process_absent,
    shell,
)
from nika.problems.support.benchmark_targets import (
    dhcp_server_client,
)
from nika.utils.logger import system_logger

logger = system_logger


def _no_listener(node: str, proto: str, port: int, service: str) -> OracleStep:
    """Evidence: ``ss`` runs on *node* and no socket listens on *proto* *port*."""
    flag = "-lnu" if proto == "udp" else "-lnt"
    return shell(
        node,
        f"if out=$(ss {flag}p 2>&1); then "
        f"echo \"$out\" | grep -E ':{port}\\b' || echo NO_LISTENER; "
        "else echo SS_FAILED; fi",
        f"Check whether {node} listens on {proto.upper()} port {port} ({service}).",
        contains(r"^NO_LISTENER$", f"ss runs and no socket listens on {proto}/{port}"),
        evidence=True,
    )


def _no_port_filter(node: str, port: int) -> OracleStep:
    """Evidence: no packet-filter rule drops *port*, ruling out an ACL block."""
    return shell(
        node,
        "if r=$(nft list ruleset 2>/dev/null); then echo NFT_READ; "
        "echo \"$r\" | grep -nE 'drop|reject' | head -20; fi; "
        "if r=$(iptables -S 2>/dev/null); then echo IPTABLES_READ; "
        "echo \"$r\" | grep -E 'DROP|REJECT' | head -20; fi",
        f"Rule out a packet filter on {node} that blocks port {port}.",
        all_of(
            contains(r"^(NFT|IPTABLES)_READ$", "the packet filter could be read"),
            lacks(
                rf"port {port}\b|dport {port}\b", f"no filter rule matches port {port}"
            ),
        ),
        evidence=True,
    )


# ==================================================================
# Problem: DNS service down
# ==================================================================


class DNSServiceDownParams(BaseModel):
    """Parameters for injecting a DNS service down fault."""

    host_name: str = Field(description="Target DNS server host name.")
    service_name: str = Field(default="named", description="Service name.")


class DNSServiceDown(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "dns_service_down"
    description = "DNS server process is down."
    symptom_desc = "Some hosts cannot access external websites."
    TAGS: str = ["dns"]
    RECORDED_ATTRS = ("service_name",)

    Params = DNSServiceDownParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.dns0}

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

    def oracle_playbook(self, params: DNSServiceDownParams) -> OraclePlaybook:
        host = params.host_name
        steps = dns_client_steps(
            self.net_env,
            host,
            contains(
                r"refused|timed out|no servers could be reached",
                "DNS queries get no answer",
            ),
            evidence=False,
        )
        steps += [
            process_absent(host, "named", f"Check for the DNS daemon on {host}."),
            _no_listener(host, "udp", 53, "DNS"),
            _no_port_filter(host, 53),
        ]
        return OraclePlaybook(
            finding=(
                f"The DNS daemon (named) on {host} is not running and nothing "
                "listens on port 53, so name resolution fails."
            ),
            steps=steps,
        )


# ==================================================================
# Problem: DHCP service down
# ==================================================================


class DHCPServiceDownParams(BaseModel):
    """Parameters for injecting a DHCP service down fault."""

    host_name: str = Field(description="Target DHCP server host name.")
    host_name_2: str = Field(default="", description="Affected client host name.")
    service_name: str = Field(default="isc-dhcp-server", description="Service name.")


class DHCPServiceDown(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "dhcp_service_down"

    description = "DHCP server process is down."
    TAGS: str = ["dhcp"]
    RECORDED_ATTRS = ("service_name",)

    Params = DHCPServiceDownParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return dhcp_server_client(ctx)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.service_name = "isc-dhcp-server"

    def root_cause_resources(self, params: DHCPServiceDownParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DHCPServiceDownParams):
        # Stop via systemd so the unit does not respawn dhcpd after pkill.
        self.runtime.systemctl(params.host_name, params.service_name, "stop")
        self.runtime.kill_process(params.host_name, "dhcpd")
        # Renew now, as clients do when their leases run out during an outage;
        # otherwise every client keeps a valid lease and nothing breaks.
        self.runtime.renew_dhcp_leases(self.runtime.list_dhcp_client_nodes())

    def verify_fault(self, params: DHCPServiceDownParams) -> dict:
        """Verify DHCP server process is not running."""
        verified = self.runtime.process_not_running(params.host_name, "dhcpd")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "service": params.service_name},
        )

    def oracle_playbook(self, params: DHCPServiceDownParams) -> OraclePlaybook:
        host = params.host_name
        steps = []
        if params.host_name_2:
            client = params.host_name_2
            steps.append(
                shell(
                    client,
                    "ip -br link show dev eth0; "
                    "ip -4 -br addr show dev eth0 | grep . || echo NO_IPV4",
                    f"Check whether DHCP client {client} holds an IPv4 lease.",
                    contains(r"^NO_IPV4$", f"{client} has no IPv4 address"),
                    attempts=3,
                    interval_sec=10,
                )
            )
        steps += [
            process_absent(host, "dhcpd", f"Check for the DHCP daemon on {host}."),
            _no_listener(host, "udp", 67, "DHCP"),
            _no_port_filter(host, 67),
        ]
        return OraclePlaybook(
            finding=(
                f"The DHCP daemon on {host} is not running and nothing listens "
                "on UDP port 67, so clients cannot obtain leases."
            ),
            steps=steps,
        )
