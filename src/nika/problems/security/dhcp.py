import ipaddress
import re

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
    DHCPD_CONF,
    addresses,
    client_subnet,
    default_gateway,
    dhcp_client_step,
    first_nameserver,
    host_ipv4,
    lease_option,
    segment_router,
    set_subnet_option,
    subnet_declared,
    subnet_option_value,
)
from nika.problems.support.dns import dns_host_for_domain
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
    shell,
)


def _probe_name(net_env) -> str:
    """A service name the clients resolve, from the scenario's web URLs."""
    urls = getattr(net_env, "web_urls", None) or []
    for url in urls:
        name = url.split("://")[-1].strip("/").split("/")[0]
        if name and not re.fullmatch(r"[\d.:]+", name):
            return name
    return "web0.local"


def _server_option_step(
    server: str, option: str, key: str, value_of, purpose: str
) -> OracleStep:
    """dhcpd.conf on *server* hands out *option* with the value a client learned."""

    def args(out):
        value = value_of(out.get(key, "")) or "0.0.0.0"
        return {
            "host_name": server,
            "command": (
                f"grep -nE '^[[:space:]]*subnet |option {option} ' {DHCPD_CONF} "
                f"| grep -B1 -F 'option {option} {value};' || echo NOT_SERVED"
            ),
            "timeout": 15,
        }

    return OracleStep(
        purpose,
        "exec_shell",
        args,
        all_of(
            contains(r"subnet [\d.]+ netmask", "a subnet declaration"),
            contains(rf"option {option} [\d.]+;", f"serves that {option} value"),
            lacks(r"^NOT_SERVED$", "the value is in the server configuration"),
        ),
        evidence=True,
    )


def _leased(output: str, label: str) -> ipaddress.IPv4Address | None:
    m = re.search(rf"^{label}=(\d+\.\d+\.\d+\.\d+)$", output, re.MULTILINE)
    return ipaddress.ip_address(m.group(1)) if m else None


def _real_owner_step(
    node: str, label: str, value_of, purpose: str, expect: Check, extra: str = ""
) -> OracleStep:
    """List *node*'s addresses next to the value the client leased."""

    def args(out):
        value = value_of(out.get("client", "")) or "none"
        return {
            "host_name": node,
            "command": (
                "ip -4 -o addr show scope global | awk '{print $2, $4}'; "
                f"echo {label}={value}{extra}"
            ),
            "timeout": 15,
        }

    return OracleStep(purpose, "exec_shell", args, expect, evidence=True)


def _learned(option: str, live) -> Check:
    def predicate(o: str) -> bool:
        value = live(o)
        return bool(value) and value == lease_option(o, option)

    return Check(
        f"the live value equals the {option} option of the DHCP lease", predicate
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

    def _real_gateway(self, client: str) -> list[OracleStep]:
        router = segment_router(self.net_env, client)
        if router is None:
            return []

        def not_router_address(o: str) -> bool:
            leased, owned = _leased(o, "LEASED_GATEWAY"), addresses(o)
            return (
                leased is not None
                and any(leased in a.network for a in owned)
                and all(leased != a.ip for a in owned)
            )

        return [
            _real_owner_step(
                router,
                "LEASED_GATEWAY",
                default_gateway,
                f"Compare the leased gateway with the addresses of {router}, the "
                f"router on the segment of {client}.",
                Check(
                    f"{router} serves the client subnet with another address, "
                    "so the leased gateway is not the subnet's router",
                    not_router_address,
                ),
            )
        ]

    def oracle_playbook(self, params: DHCPSpoofedGatewayParams) -> OraclePlaybook:
        server, client = params.host_name, params.host_name_2

        def gateway_probe(out):
            gw = default_gateway(out.get("client", "")) or "0.0.0.0"
            return {
                "host_name": client,
                "command": f"ping -c 2 -W 1 {gw} 2>&1 | tail -2; ip neigh show {gw}",
                "timeout": 15,
            }

        return OraclePlaybook(
            finding=(
                f"The DHCP server {server} hands out a router option that is not "
                f"the segment's gateway, so {client} and its subnet peers install "
                "a dead default route."
            ),
            steps=[
                dhcp_client_step(
                    client,
                    "ip -4 -o addr show dev eth0 scope global; ip -4 route show default",
                    f"Compare the default route of {client} with its DHCP lease.",
                    _learned("routers", default_gateway),
                    evidence=True,
                    save_as="client",
                    attempts=3,
                ),
                OracleStep(
                    "Probe the learned default gateway.",
                    "exec_shell",
                    gateway_probe,
                    all_of(
                        contains(r" 0 received|100% packet loss", "no reply"),
                        lacks(r"lladdr", "no node answers ARP for it"),
                    ),
                    evidence=True,
                ),
                *self._real_gateway(client),
                _server_option_step(
                    server,
                    "routers",
                    "client",
                    default_gateway,
                    f"Find the router option in the subnet declarations on {server}.",
                ),
            ],
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

    def _real_dns(self, name: str) -> list[OracleStep]:
        dns = dns_host_for_domain(self.net_env, name.split(".", 1)[-1])
        if dns is None:
            return []

        def not_dns_address(o: str) -> bool:
            leased, owned = _leased(o, "LEASED_DNS"), addresses(o)
            return (
                leased is not None
                and bool(owned)
                and all(leased != a.ip for a in owned)
                and "status: NOERROR" in o
            )

        return [
            _real_owner_step(
                dns,
                "LEASED_DNS",
                first_nameserver,
                f"Check that the lab DNS server {dns} answers for {name} and "
                "whether it owns the leased resolver address.",
                Check(
                    f"{dns} answers but does not own the leased resolver address",
                    not_dns_address,
                ),
                extra=(
                    f"; dig +time=2 +tries=1 @127.0.0.1 {name} 2>&1 | grep -E 'status:'"
                ),
            )
        ]

    def oracle_playbook(self, params: DHCPSpoofedDNSParams) -> OraclePlaybook:
        server, client = params.host_name, params.host_name_2
        name = _probe_name(self.net_env)

        def lookup(out):
            ns = first_nameserver(out.get("client", "")) or "0.0.0.0"
            return {
                "host_name": client,
                "command": f"dig +time=2 +tries=1 @{ns} {name} 2>&1 | head -12",
                "timeout": 15,
            }

        return OraclePlaybook(
            finding=(
                f"The DHCP server {server} hands out a DNS server option that "
                f"answers no queries, so {client} and its subnet peers cannot "
                "resolve names."
            ),
            steps=[
                dhcp_client_step(
                    client,
                    "grep -E '^(nameserver|search)' /etc/resolv.conf",
                    f"Compare the resolver of {client} with its DHCP lease.",
                    _learned("domain-name-servers", first_nameserver),
                    evidence=True,
                    save_as="client",
                    attempts=3,
                ),
                OracleStep(
                    f"Query the learned resolver for {name}.",
                    "exec_shell",
                    lookup,
                    contains(
                        r"timed out|no servers could be reached|connection refused",
                        "the resolver does not answer",
                    ),
                    evidence=True,
                ),
                *self._real_dns(name),
                _server_option_step(
                    server,
                    "domain-name-servers",
                    "client",
                    first_nameserver,
                    f"Find the DNS server option in the subnet declarations on {server}.",
                ),
            ],
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

    def oracle_playbook(self, params: DHCPSpoofedSubnetParams) -> OraclePlaybook:
        server, client = params.host_name, params.host_name_2

        def mask_from_lease(o: str) -> bool:
            addr = host_ipv4(o.split("dhclient=")[0])
            mask, gw = lease_option(o, "subnet-mask"), lease_option(o, "routers")
            if not (addr and mask and gw):
                return False
            prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
            return (
                addr.network.prefixlen == prefix
                and ipaddress.ip_address(gw) not in addr.network
            )

        def spoofed_mask(o: str) -> bool:
            blocks = re.findall(
                r"subnet [\d.]+ netmask ([\d.]+)\s*\{(.*?)\}", o, re.DOTALL
            )
            for netmask, body in blocks:
                m = re.search(r"option subnet-mask ([\d.]+);", body)
                if m and m.group(1) != netmask:
                    return True
            return False

        return OraclePlaybook(
            finding=(
                f"The DHCP server {server} hands out a subnet mask narrower than "
                f"the declared subnet, so {client} and its subnet peers lose their "
                "on-link gateway."
            ),
            steps=[
                dhcp_client_step(
                    client,
                    "ip -4 -o addr show dev eth0 scope global; ip -4 route",
                    f"Compare the prefix of {client} with its DHCP lease and gateway.",
                    Check(
                        "the leased mask is the live prefix and leaves the leased "
                        "gateway off-link",
                        mask_from_lease,
                    ),
                    evidence=True,
                    attempts=3,
                ),
                shell(
                    server,
                    f"grep -vE '^[[:space:]]*(#|$)' {DHCPD_CONF} | head -120",
                    f"Inspect the subnet declarations on {server}.",
                    Check(
                        "a subnet declaration overrides its netmask with a "
                        "different subnet-mask option",
                        spoofed_mask,
                    ),
                    evidence=True,
                ),
            ],
        )
