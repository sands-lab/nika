import ipaddress
import re
import shlex
from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.support.inject_resolve import (
    derive_incorrect_ip,
    derive_wrong_gateway,
    unused_address_near,
)
from nika.problems.rca import node_resource
from nika.problems.rca.inventory import interface_on
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import (
    choice_distinct,
    device_interfaces,
    endpoint_target,
    require_distinct_hosts,
)
from nika.problems.support.dhcp import (
    DHCP_LEASE_CMD,
    default_gateway,
    dhcp_client_step,
    first_nameserver,
    lease_option,
)
from nika.problems.addressing_neighbor_naming.dns import (
    _parse_web_url,
)
from nika.problems.rca.inventory import iter_link_termination_points
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
from nika.utils.logger import system_logger


# ------------------------------------------------------------------
# Oracle helpers: running interface config against the boot config
# ------------------------------------------------------------------

# Kathara runs each node's startup script under ``set -x`` into this log, so
# it records the addresses and routes the node was provisioned with.
BOOT_LOG = "/var/log/startup.log"
_BOOT_MARK = "--- boot config"


def running_vs_boot_cmd(intf: str = "eth0") -> str:
    """Print the live IPv4 address and default route, then the boot config."""
    return (
        f"ip -br link show dev {intf}; "
        f"ip -4 -o addr show dev {intf} scope global; ip -4 route show default; "
        f"echo '{_BOOT_MARK}'; "
        f"grep -E '^\\++ *ip .*(addr|address|route) (add|replace) ' {BOOT_LOG} "
        "2>/dev/null | head -20"
    )


def _split(output: str) -> tuple[str, str]:
    live, _, boot = output.partition(_BOOT_MARK)
    return live, boot


def live_ipv4(output: str) -> ipaddress.IPv4Interface | None:
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+/\d+)", _split(output)[0])
    return ipaddress.IPv4Interface(m.group(1)) if m else None


def boot_ipv4(output: str, intf: str = "eth0") -> ipaddress.IPv4Interface | None:
    m = re.search(
        rf"ip (?:-4 )?addr(?:ess)? (?:add|replace) (\d+\.\d+\.\d+\.\d+/\d+)"
        rf"(?: \S+ \S+)* dev {re.escape(intf)}\b",
        _split(output)[1],
    )
    return ipaddress.IPv4Interface(m.group(1)) if m else None


def boot_gateway(output: str) -> str | None:
    m = re.search(
        r"ip (?:-4 )?route (?:add|replace) (?:default|0\.0\.0\.0/0) via (\S+)",
        _split(output)[1],
    )
    return m.group(1) if m else None


def running_vs_boot(
    node: str, purpose: str, expect: Check, *, intf: str = "eth0", **kw
) -> OracleStep:
    return shell(node, running_vs_boot_cmd(intf), purpose, expect, evidence=True, **kw)


def segment_peer(net_env, host: str, intf: str = "eth0") -> tuple[str, str] | None:
    """A non-host node (the gateway) on the same link as *host*:*intf*."""
    needle = f"{host}:{intf}"
    hosts = set(getattr(net_env, "hosts", None) or [])
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = [str(ep) for ep in tps]
        if needle not in endpoints:
            continue
        peers = [ep.split(":", 1) for ep in endpoints if ep != needle]
        peers.sort(key=lambda p: p[0] in hosts)
        return (peers[0][0], peers[0][1]) if peers else None
    return None


def _read_host_ipv4(
    runtime,
    host_name: str,
    intf_name: str = "eth0",
) -> str | None:
    """Return host IPv4 CIDR on ``intf_name``, with exec fallback."""
    ip = runtime.get_host_ip(host_name, intf_name, with_prefix=True)
    if ip:
        return ip
    line = runtime.exec(
        host_name,
        f"ip -4 -o addr show dev {intf_name} scope global 2>/dev/null | head -1",
    ).strip()
    match = re.search(r"inet\s+(\S+)", line)
    return match.group(1) if match else None


# Route state that ``ip route show`` prints but ``ip route replace`` rejects.
_ROUTE_STATE_FLAGS = frozenset({"linkdown", "dead", "offload", "trap", "rt_offload"})


def _inject_ip_change(
    runtime,
    *,
    host_name: str,
    old_ip: str,
    new_ip: str,
    intf_name: str,
    new_gateway: str | None = None,
) -> None:
    # Deleting the last IPv4 address also flushes the interface's static routes
    # and permanent neighbours (the on-link gateway of a /32 host). Re-apply
    # them, as a config-managed host would, so only the address changes.
    routes = runtime.exec(host_name, f"ip -4 route show dev {intf_name}")
    neighs = runtime.exec(host_name, f"ip -4 neigh show dev {intf_name} nud permanent")
    runtime.exec(host_name, f"ip addr del {old_ip} dev {intf_name}")
    runtime.exec(host_name, f"ip addr add {new_ip} dev {intf_name}")
    for line in neighs.splitlines():
        match = re.match(r"(\S+) lladdr (\S+)", line.strip())
        if match:
            runtime.exec(
                host_name,
                f"ip neigh replace {match[1]} lladdr {match[2]} "
                f"dev {intf_name} nud permanent",
            )
    static: list[str] = []
    for line in routes.splitlines():
        line = line.strip()
        if line.startswith("nexthop") and static:
            static[-1] += f" {line}"  # a multipath route's next hops
        elif line:
            static.append(line)
    old_addr, new_addr = old_ip.split("/")[0], new_ip.split("/")[0]
    # On-link routes first, so the gateways of the ``via`` routes resolve.
    for route in sorted(static, key=lambda r: " via " in r):
        if "proto kernel" in route:
            continue
        words = [w for w in route.split() if w not in _ROUTE_STATE_FLAGS]
        words = [
            new_addr if w == old_addr and i and words[i - 1] == "src" else w
            for i, w in enumerate(words)
        ]
        if "dev" not in words:
            words += ["dev", intf_name]
        runtime.exec(host_name, f"ip route replace {' '.join(words)}")
    if new_gateway:
        onlink = " onlink" if new_ip.endswith("/32") else ""
        runtime.exec(
            host_name,
            f"ip route replace default via {new_gateway} dev {intf_name}{onlink}",
        )


# ==========================================
# Problem: Host missing IP address
# ==========================================


class HostMissingIPParams(BaseModel):
    """Parameters for injecting a params.host_name-missing-IP fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")


class HostMissingIP(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "host_missing_ip"
    root_cause_owner = "interface"
    description = "Host interface has no IP address."
    TAGS: str = ["pc"]
    RECORDED_ATTRS = ("intf_name",)

    Params = HostMissingIPParams
    BENCHMARK_COORDINATES = frozenset({"link_down"})

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return endpoint_target(ctx, with_intf=True)

    @classmethod
    def coordinate_benchmark_inject(cls, ctx, params_by_problem):
        # Keep the host fault off the downed link's endpoint so both are visible.
        net_env = ctx.net_env
        link = dict(params_by_problem["link_down"])
        host = dict(params_by_problem["host_missing_ip"])
        if link.get("host_name") == host.get("host_name"):
            web_hosts = list((getattr(net_env, "servers", None) or {}).get("web") or [])
            if web_hosts:
                host["host_name"] = web_hosts[0]
                ifaces = device_interfaces(net_env).get(web_hosts[0]) or ["eth0"]
                host["intf_name"] = ifaces[0]
            else:
                hosts = list(net_env.hosts or [])
                alt = next((h for h in hosts if h != link.get("host_name")), None)
                if alt is not None:
                    host["host_name"] = alt
                    ifaces = device_interfaces(net_env).get(alt) or ["eth0"]
                    host["intf_name"] = ifaces[0]
        params_by_problem["host_missing_ip"] = host
        return params_by_problem

    symptom_desc = (
        "Some hosts are unable to communicate with other devices in the network."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self.intf_name = "eth0"

    def root_cause_resources(self, params: HostMissingIPParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: HostMissingIPParams):
        real_ip = self.runtime.get_host_ip(
            params.host_name, params.intf_name, with_prefix=True
        )
        real_gateway = self.runtime.get_default_gateway(params.host_name)
        self.runtime.exec(
            params.host_name, f"ip addr del {real_ip} dev {params.intf_name}"
        )
        self.logger.info(
            f"Injected missing IP on {params.host_name} from {real_ip} and gateway {real_gateway}."
        )

    def verify_fault(self, params: HostMissingIPParams) -> dict:
        """Verify that the params.host_name has no global IPv4 address on the interface."""
        ip_line = self.runtime.exec(
            params.host_name, f"ip -4 -o addr show dev {params.intf_name} scope global"
        ).strip()
        verified = "inet " not in ip_line
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "intf": params.intf_name,
                "ip_line": ip_line,
            },
        )

    def oracle_playbook(self, params: HostMissingIPParams) -> OraclePlaybook:
        host, intf = params.host_name, params.intf_name
        return OraclePlaybook(
            finding=(
                f"{host}:{intf} is up but has lost the IPv4 address it was "
                "provisioned with, so the host cannot send or receive IP traffic."
            ),
            steps=[
                running_vs_boot(
                    host,
                    f"Compare the running address of {host}:{intf} with its boot config.",
                    all_of(
                        contains(rf"^{re.escape(intf)}(@\S+)?\s+UP\b", f"{intf} is up"),
                        Check(
                            f"{intf} has no IPv4 address now",
                            lambda o: live_ipv4(o) is None,
                        ),
                        Check(
                            f"the boot config assigns {intf} a static address",
                            lambda o: boot_ipv4(o, intf) is not None,
                        ),
                    ),
                    intf=intf,
                ),
                shell(
                    host,
                    DHCP_LEASE_CMD,
                    f"Check that {host} is not waiting for a DHCP lease.",
                    contains(r"^dhclient=none", "no DHCP client runs"),
                    evidence=True,
                ),
            ],
        )


# ==========================================
""" Problem: Host IP conflict """


class HostIPConflictParams(BaseModel):
    """Parameters for injecting a params.host_name IP conflict fault."""

    host_name: str = Field(description="Source params.host_name whose IP is copied.")
    host_name_2: str = Field(description="Target params.host_name to misconfigure.")


class HostIPConflict(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "host_ip_conflict"
    root_cause_owner = "interface"
    description = "Two hosts are configured with the same IP address."
    TAGS: str = ["pc"]

    Params = HostIPConflictParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        servers = ctx.servers
        conflict_pool = list(ctx.host_pool)
        for bucket in (servers.get("web") or [], servers.get("dns") or []):
            for name in bucket:
                if name not in conflict_pool:
                    conflict_pool.append(name)
        if len(conflict_pool) < 2:
            # Fall back to all declared hosts + web servers from inventory.
            conflict_pool = list(
                dict.fromkeys(
                    list(ctx.hosts)
                    + list(servers.get("web") or [])
                    + list(servers.get("dns") or [])
                )
            )
        pair = choice_distinct(ctx.rng, conflict_pool, ctx.host0)
        if pair[0] == pair[1] and len(conflict_pool) >= 2:
            pair = [conflict_pool[0], conflict_pool[1]]
        return {"host_name": pair[0], "host_name_2": pair[1]}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        require_distinct_hosts(ctx, cls.root_cause_name, inject)

    symptom_desc = "Some hosts experience intermittent connectivity issues."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: HostIPConflictParams):
        return [interface_on(self.net_env, params.host_name_2, "eth0")]

    def inject_fault(self, params: HostIPConflictParams):
        src_host = params.host_name
        dst_host = params.host_name_2
        _inject_ip_change(
            self.runtime,
            host_name=dst_host,
            old_ip=self.runtime.get_host_ip(dst_host, "eth0", with_prefix=True),
            new_ip=self.runtime.get_host_ip(src_host, "eth0", with_prefix=True),
            intf_name="eth0",
            new_gateway=self.runtime.get_default_gateway(src_host),
        )

    def verify_fault(self, params: HostIPConflictParams) -> dict:
        """Verify both hosts share the same eth0 IP (conflict)."""
        host_a = params.host_name
        host_b = params.host_name_2
        cmd = "ip -4 -o addr show dev eth0 scope global | awk '/inet /{print $4}'"
        ip_a_raw = self.runtime.exec(host_a, cmd).strip()
        ip_b_raw = self.runtime.exec(host_b, cmd).strip()
        ip_a = ip_a_raw.split("/")[0] if ip_a_raw else ""
        ip_b = ip_b_raw.split("/")[0] if ip_b_raw else ""
        verified = bool(ip_a) and ip_a == ip_b
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host_a": host_a, "host_b": host_b, "ip_a": ip_a, "ip_b": ip_b},
        )

    def oracle_playbook(self, params: HostIPConflictParams) -> OraclePlaybook:
        owner, dup = params.host_name, params.host_name_2

        def duplicate(out):
            ip = live_ipv4(out.get("owner", ""))
            addr = str(ip.ip) if ip else "0.0.0.0"
            return {
                "host_name": dup,
                "command": (
                    f"{running_vs_boot_cmd()}; ip -4 -o addr show dev eth0 "
                    f"| grep -qwF '{addr}' && echo SAME_ADDRESS={addr} "
                    f"|| echo DIFFERENT_ADDRESS"
                ),
                "timeout": 15,
            }

        def drifted(o: str) -> bool:
            live, boot = live_ipv4(o), boot_ipv4(o)
            return bool(live and boot) and live.ip != boot.ip

        return OraclePlaybook(
            finding=(
                f"{dup}:eth0 is configured with the address of {owner}, so the "
                "two hosts conflict and traffic for that address reaches either."
            ),
            steps=[
                running_vs_boot(
                    owner,
                    f"Read the running and boot address of {owner}.",
                    Check(
                        f"{owner} runs the address it was provisioned with",
                        lambda o: bool(live_ipv4(o)) and live_ipv4(o) == boot_ipv4(o),
                    ),
                    save_as="owner",
                ),
                OracleStep(
                    f"Check whether {dup} holds the same address, and against its "
                    "own boot config.",
                    "exec_shell",
                    duplicate,
                    all_of(
                        contains(r"^SAME_ADDRESS=", f"{dup} holds {owner}'s address"),
                        Check(f"{dup} was provisioned with another address", drifted),
                    ),
                    evidence=True,
                ),
            ],
        )


# ==========================================
# Problem: Incorrect Host IP
# ==========================================


class HostIncorrectIPParams(BaseModel):
    """Parameters for injecting an incorrect params.host_name IP fault."""

    host_name: str = Field(description="Target host name.")
    incorrect_ip: Optional[str] = Field(
        default=None,
        description="Incorrect CIDR IP. Derived at inject time if omitted.",
    )


class HostIncorrectIP(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "host_incorrect_ip"
    root_cause_owner = "interface"
    description = "Host IP address is incorrect."
    TAGS: str = ["pc"]

    Params = HostIncorrectIPParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return endpoint_target(ctx)

    symptom_desc = "Some hosts seem to be unreachable in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original_ip: str | None = None
        self._injected_ip: str | None = None

    def root_cause_resources(self, params: HostIncorrectIPParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: HostIncorrectIPParams):
        old_ip = _read_host_ipv4(self.runtime, params.host_name, "eth0")
        self._original_ip = old_ip
        incorrect_ip = params.incorrect_ip or derive_incorrect_ip(
            self.runtime, params.host_name
        )
        self._injected_ip = incorrect_ip
        _inject_ip_change(
            self.runtime,
            host_name=params.host_name,
            old_ip=old_ip,
            new_ip=incorrect_ip,
            intf_name="eth0",
            new_gateway=self.runtime.get_default_gateway(params.host_name),
        )

    def verify_fault(self, params: HostIncorrectIPParams) -> dict:
        """Verify that the params.host_name eth0 IP differs from the original address at inject time."""
        ip_line = self.runtime.exec(
            params.host_name, "ip -4 -o addr show dev eth0 scope global"
        ).strip()
        current_ip = None
        if "inet " in ip_line:
            parts = ip_line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    current_ip = parts[i + 1]
                    break
        original = self._original_ip
        injected = self._injected_ip or params.incorrect_ip
        current_addr = current_ip.split("/")[0] if current_ip else None
        original_addr = original.split("/")[0] if original else None
        injected_addr = injected.split("/")[0] if injected else None
        if original_addr:
            verified = bool(current_addr) and current_addr != original_addr
        else:
            # No in-memory original (fresh process): require the live address
            # to be the configured incorrect one.
            verified = bool(current_addr) and current_addr == injected_addr
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "ip_line": ip_line,
                "original_ip": original,
                "injected_ip": injected,
                "current_ip": current_ip,
            },
        )

    def oracle_playbook(self, params: HostIncorrectIPParams) -> OraclePlaybook:
        host = params.host_name

        def wrong_address(o: str) -> bool:
            live, boot = live_ipv4(o), boot_ipv4(o)
            return (
                bool(live and boot)
                and live.ip != boot.ip
                and live.network.prefixlen == boot.network.prefixlen
            )

        def unique(out):
            ip = live_ipv4(out.get("cfg", ""))
            addr = str(ip.ip) if ip else "0.0.0.0"
            return {
                "host_name": host,
                "command": (
                    f"arping -D -c 2 -w 3 -I eth0 {addr} 2>&1 | tail -3; "
                    f"ip neigh show dev eth0 | head -5"
                ),
                "timeout": 15,
            }

        return OraclePlaybook(
            finding=(
                f"{host}:eth0 runs an address other than the one it was "
                "provisioned with, so traffic for its real address goes nowhere."
            ),
            steps=[
                running_vs_boot(
                    host,
                    f"Compare the running address of {host}:eth0 with its boot config.",
                    Check(
                        "the running address differs from the provisioned one "
                        "(same prefix length)",
                        wrong_address,
                    ),
                    save_as="cfg",
                ),
                OracleStep(
                    f"Check that no other node answers for the running address of {host}.",
                    "exec_shell",
                    unique,
                    # iputils prints "Received 0 response(s)"; Habets arping
                    # prints "100% unanswered" or "100% packet loss".
                    contains(
                        r"Received 0 response|100% (unanswered|packet loss)",
                        "no other node answers duplicate address detection",
                    ),
                    evidence=True,
                ),
            ],
        )


# ==========================================
# Problem: Incorrect Host Gateway
# ==========================================


class HostIncorrectGatewayParams(BaseModel):
    """Parameters for injecting an incorrect params.host_name gateway fault."""

    host_name: str = Field(description="Target host name.")
    new_gateway: Optional[str] = Field(
        default=None,
        description="Incorrect gateway IP. Derived at inject time if omitted.",
    )


class HostIncorrectGateway(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "path_ping"
    symptom_class = "unreachable"
    root_cause_name: str = "host_incorrect_gateway"
    description = (
        "The host's own default route points at an incorrect gateway "
        "(set locally, not learned from DHCP)."
    )
    TAGS: str = ["pc", "frr"]

    Params = HostIncorrectGatewayParams
    BENCHMARK_COORDINATES = frozenset({"dns_record_error"})

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return endpoint_target(ctx)

    @classmethod
    def coordinate_benchmark_inject(cls, ctx, params_by_problem):
        # Misroute a client other than the DNS server so both faults are visible.
        net_env = ctx.net_env
        dns = dict(params_by_problem["dns_record_error"])
        gw = dict(params_by_problem["host_incorrect_gateway"])
        if ctx.scenario == "campus_lan":
            dns["host_name"] = "dns_server"
            gw["host_name"] = "pc_1_1_1_1"
        elif dns.get("host_name") == gw.get("host_name"):
            hosts = list(net_env.hosts or [])
            corp = [h for h in hosts if h.endswith("_corp_pc")]
            alt = next((h for h in corp if h != dns.get("host_name")), None)
            if alt is None:
                alt = next((h for h in hosts if h != dns.get("host_name")), None)
            if alt is not None:
                gw["host_name"] = alt
        params_by_problem["dns_record_error"] = dns
        params_by_problem["host_incorrect_gateway"] = gw
        return params_by_problem

    symptom_desc = "Some hosts seem to be unreachable in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._injected_gateway: str | None = None

    def root_cause_resources(self, params: HostIncorrectGatewayParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HostIncorrectGatewayParams):
        new_gateway = params.new_gateway or derive_wrong_gateway(
            self.runtime, params.host_name
        )
        self._injected_gateway = new_gateway
        _inject_ip_change(
            self.runtime,
            host_name=params.host_name,
            old_ip=self.runtime.get_host_ip(params.host_name, "eth0", with_prefix=True),
            new_ip=self.runtime.get_host_ip(params.host_name, "eth0", with_prefix=True),
            intf_name="eth0",
            new_gateway=new_gateway,
        )

    def verify_fault(self, params: HostIncorrectGatewayParams) -> dict:
        """Verify that the default route uses the injected wrong gateway."""
        route_line = self.runtime.exec(
            params.host_name, "ip route show default"
        ).strip()
        expected_gateway = params.new_gateway or self._injected_gateway
        if not expected_gateway:
            # Fresh instance after workflow inject: .254 derivation is idempotent
            # once the wrong gateway is already installed.
            try:
                expected_gateway = derive_wrong_gateway(self.runtime, params.host_name)
            except ValueError:
                expected_gateway = None
        verified = bool(expected_gateway) and expected_gateway in route_line
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "route_line": route_line,
                "expected_gateway": expected_gateway,
            },
        )

    def oracle_playbook(self, params: HostIncorrectGatewayParams) -> OraclePlaybook:
        host = params.host_name

        def static_wrong_gateway(o: str) -> bool:
            gw = default_gateway(o.split(_BOOT_MARK)[0])
            references = {boot_gateway(o), lease_option(o, "routers")} - {None}
            return bool(gw) and bool(references) and gw not in references

        def gateway_probe(out):
            gw = default_gateway(out.get("cfg", "").split(_BOOT_MARK)[0])
            gw = gw or "0.0.0.0"
            return {
                "host_name": host,
                "command": f"ping -c 2 -W 1 {gw} 2>&1 | tail -2; ip neigh show {gw}",
                "timeout": 15,
            }

        return OraclePlaybook(
            finding=(
                f"The default route on {host} points at a local, statically set "
                "next hop that no router answers for, so off-subnet traffic is lost."
            ),
            steps=[
                shell(
                    host,
                    f"{running_vs_boot_cmd()}; {DHCP_LEASE_CMD}",
                    f"Compare the default route of {host} with its boot config and "
                    "any DHCP lease.",
                    Check(
                        "a provisioned or leased gateway is known and the "
                        "default gateway is neither",
                        static_wrong_gateway,
                    ),
                    evidence=True,
                    save_as="cfg",
                ),
                OracleStep(
                    "Probe the configured default gateway.",
                    "exec_shell",
                    gateway_probe,
                    all_of(
                        contains(r" 0 received|100% packet loss", "no reply"),
                        lacks(r"lladdr", "no node answers ARP for it"),
                    ),
                    evidence=True,
                ),
            ],
        )


# ==========================================
# Problem: Incorrect Host netmask
# ==========================================


class HostIncorrectNetmaskParams(BaseModel):
    """Parameters for injecting an incorrect params.host_name netmask fault."""

    host_name: str = Field(description="Target host name.")
    netmask_prefix: int = Field(default=8, description="Incorrect prefix length.")


class HostIncorrectNetmask(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "host_incorrect_netmask"
    root_cause_owner = "interface"
    description = "Host netmask/prefix length is incorrect."
    TAGS: str = ["pc", "frr"]

    Params = HostIncorrectNetmaskParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = endpoint_target(ctx)
        params["netmask_prefix"] = "8"
        return params

    symptom_desc = "Some hosts seem to be unreachable in the network."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.netmask_prefix = 8

    def root_cause_resources(self, params: HostIncorrectNetmaskParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: HostIncorrectNetmaskParams):
        old_ip = self.runtime.get_host_ip(params.host_name, "eth0", with_prefix=True)
        ip_part = old_ip.split("/")[0]
        new_ip = f"{ip_part}/{params.netmask_prefix}"
        _inject_ip_change(
            self.runtime,
            host_name=params.host_name,
            old_ip=old_ip,
            new_ip=new_ip,
            intf_name="eth0",
            new_gateway=self.runtime.get_default_gateway(params.host_name),
        )

    def verify_fault(self, params: HostIncorrectNetmaskParams) -> dict:
        """Verify that eth0 carries the injected ``netmask_prefix``."""
        ip_line = self.runtime.exec(
            params.host_name, "ip -4 -o addr show dev eth0 scope global"
        ).strip()
        prefix = None
        if "inet " in ip_line:
            parts = ip_line.split()
            for i, p in enumerate(parts):
                if p == "inet" and i + 1 < len(parts):
                    cidr = parts[i + 1]
                    if "/" in cidr:
                        prefix = int(cidr.split("/")[1])
                    break
        verified = prefix == params.netmask_prefix
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "ip_line": ip_line,
                "expected_prefix": params.netmask_prefix,
                "actual_prefix": prefix,
            },
        )

    def oracle_playbook(self, params: HostIncorrectNetmaskParams) -> OraclePlaybook:
        host, prefix = params.host_name, int(params.netmask_prefix)

        def wrong_prefix(o: str) -> bool:
            live, boot = live_ipv4(o), boot_ipv4(o)
            return (
                bool(live and boot)
                and live.ip == boot.ip
                and live.network.prefixlen == prefix != boot.network.prefixlen
                and lease_option(o, "subnet-mask") is None
            )

        steps = [
            shell(
                host,
                f"{running_vs_boot_cmd()}; {DHCP_LEASE_CMD}",
                f"Compare the running prefix of {host}:eth0 with its boot config.",
                Check(
                    f"eth0 runs /{prefix} instead of its provisioned prefix, "
                    "not from a DHCP lease",
                    wrong_prefix,
                ),
                evidence=True,
            )
        ]
        peer = segment_peer(self.net_env, host)
        if peer is not None:
            node, intf = peer
            steps.append(
                shell(
                    node,
                    f"ip -4 -o addr show dev {shlex.quote(intf)} scope global",
                    f"Read the prefix that {node}:{intf} uses on the same segment.",
                    all_of(
                        contains(r"inet \d+\.\d+\.\d+\.\d+/\d+", "it has an address"),
                        lacks(rf"inet \S+/{prefix}\b", f"its prefix is not /{prefix}"),
                    ),
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=(
                f"{host}:eth0 is configured with a /{prefix} netmask, so it treats "
                "off-subnet destinations as on-link and never routes them."
            ),
            steps=steps,
        )


# ==========================================
# Problem: Incorrect Host DNS resolvers
# =========================================


class HostIncorrectDNSParams(BaseModel):
    """Parameters for injecting an incorrect DNS resolver fault."""

    host_name: str = Field(description="Target host name.")
    fake_dns_ip: str | None = Field(
        default=None,
        description=(
            "Incorrect DNS IP. Defaults to an unused address next to the host's "
            "real resolver, like a stale or mistyped resolver entry."
        ),
    )


class HostIncorrectDNS(ProblemBase):
    failure_domain = FailureDomain.ADDRESSING_NEIGHBOR_NAMING
    symptom_probe = "http_by_name"
    symptom_class = "unreachable"
    root_cause_name: str = "host_incorrect_dns"
    description = "Host is configured with an incorrect DNS resolver."
    TAGS: str = ["dns"]

    Params = HostIncorrectDNSParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return endpoint_target(ctx)

    symptom_desc = "Some hosts are unable to access web services."

    def root_cause_resources(self, params: HostIncorrectDNSParams):
        return [node_resource(params.host_name)]

    def _resolver(self, params: HostIncorrectDNSParams, resolv: str) -> str:
        if params.fake_dns_ip:
            return params.fake_dns_ip
        servers = re.findall(r"^\s*nameserver\s+(\d+\.\d+\.\d+\.\d+)", resolv, re.M)
        real = next((ip for ip in servers if not ip.startswith("127.")), None)
        real = real or self.runtime.get_default_gateway(params.host_name)
        if not real:
            raise ValueError(f"No resolver or gateway on {params.host_name}")
        return unused_address_near(self.runtime, real)

    def inject_fault(self, params: HostIncorrectDNSParams):
        resolv = self.runtime.exec(params.host_name, "cat /etc/resolv.conf 2>/dev/null")
        fake = self._resolver(params, resolv)
        self._fake_dns_ip = fake
        # Swap only the nameserver entries; search and options stay as they were.
        lines = [
            line for line in resolv.splitlines() if not line.startswith("nameserver")
        ]
        self.runtime.write_file(
            params.host_name,
            "/etc/resolv.conf",
            "\n".join([*lines, f"nameserver {fake}"]) + "\n",
        )
        self.runtime.exec(
            params.host_name,
            # dhclient rewrites resolv.conf at lease renewal. Keep the same
            # incorrect resolver in its renewal path on DHCP-backed hosts.
            "if test -d /etc/dhcp/dhclient-enter-hooks.d; then "
            "printf '%s\\n' 'new_domain_name_servers=\""
            f"{fake}"
            "\"' > /etc/dhcp/dhclient-enter-hooks.d/nameservers; fi; "
            # Drop static hosts overrides so name lookups must use DNS.
            "sed -i -E '/\\sweb0\\.(local|pod0)\\s*$/d' /etc/hosts 2>/dev/null || true; "
            "sed -i -E '/\\swebserver/d' /etc/hosts 2>/dev/null || true",
        )

    def verify_fault(self, params: HostIncorrectDNSParams) -> dict:
        """Verify the incorrect-DNS fault by checking /etc/resolv.conf contains the fake DNS IP."""
        resolv = self.runtime.exec(
            params.host_name, "cat /etc/resolv.conf 2>/dev/null || echo ''"
        )
        fake = params.fake_dns_ip or getattr(self, "_fake_dns_ip", None)
        verified = bool(fake) and f"nameserver {fake}" in resolv
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "fake_dns_ip": fake,
                "resolv_conf": resolv.strip(),
            },
        )

    def oracle_playbook(self, params: HostIncorrectDNSParams) -> OraclePlaybook:
        host = params.host_name
        site, domain = next(
            map(_parse_web_url, getattr(self.net_env, "web_urls", None) or []),
            ("web0", "local"),
        )
        name = f"{site}.{domain}"
        dns = dns_host_for_domain(self.net_env, domain)

        def local_resolver(o: str) -> bool:
            ns = first_nameserver(o)
            return bool(ns) and ns != lease_option(o, "domain-name-servers")

        def lookup(out):
            ns = first_nameserver(out.get("cfg", "")) or "0.0.0.0"
            return {
                "host_name": host,
                "command": f"dig +time=2 +tries=1 @{ns} {name} 2>&1 | head -12",
                "timeout": 15,
            }

        steps = [
            dhcp_client_step(
                host,
                "grep -E '^(nameserver|search)' /etc/resolv.conf",
                f"Compare the resolver configured on {host} with any DHCP lease.",
                Check(
                    "the resolver is set locally, not learned from DHCP",
                    local_resolver,
                ),
                evidence=True,
                save_as="cfg",
            ),
            OracleStep(
                f"Query the configured resolver for {name}.",
                "exec_shell",
                lookup,
                contains(
                    r"timed out|no servers could be reached|connection refused",
                    "the configured resolver does not answer",
                ),
                evidence=True,
            ),
        ]
        if dns:
            steps.append(
                shell(
                    dns,
                    f"dig +time=2 +tries=1 @127.0.0.1 {name} 2>&1 "
                    "| grep -E 'status:|IN\\s+A'",
                    f"Check that the lab DNS server {dns} answers for {name}.",
                    contains(r"status: NOERROR", "the real DNS server answers"),
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=(
                f"{host} is configured locally with a resolver address that runs "
                "no DNS service, so its name lookups fail while DNS itself works."
            ),
            steps=steps,
        )
