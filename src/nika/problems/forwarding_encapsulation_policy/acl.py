import re

from pydantic import BaseModel, Field

from nika.net_env.net_env_pool import get_probe_path
from nika.net_env.verify import frr_bgp_has_established_session
from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import (
    arp_l2_endpoint_host,
    first,
    prefer_named,
)
from nika.problems.support.compatible_columns import NON_K8S_HOST_COLUMNS
from nika.problems.support.dns import dns_client_steps
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    http_probe,
    lacks,
    ping_works,
    process_running,
    shell,
    vtysh,
)
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops
from nika.runtime.base import RuntimeCapabilityError


def _verify_nft_drop(
    problem: ProblemBase, host_name: str, *rules: str, family: str = "inet"
) -> dict:
    """Verify every ``add_nft_drop_rule`` rule is live, matched exactly."""
    present = {
        rule: problem.runtime.nft_drop_rule_present(host_name, rule, family=family)
        for rule in rules
    }
    return build_verify_result(
        fault_type=problem.root_cause_name,
        verified=all(present.values()),
        details={"host": host_name, "family": family, "rules_present": present},
    )


# Default IPv4 gateway of the node the command runs on.
_GW = "gw=$(ip -4 route show default | awk '{print $3; exit}')"


def _nft_drops(node: str, expect: Check, purpose: str) -> OracleStep:
    """Evidence: the node's nftables ruleset (tables, chains, drop rules)."""
    return shell(
        node,
        "nft list ruleset 2>&1 | grep -nE 'table|chain|hook|drop|reject' | head -60",
        purpose,
        expect,
        evidence=True,
    )


def _nft_table_rule(family: str, rule: str, description: str) -> Check:
    """A rule matching *rule* sits in an nftables table of *family*."""
    rule_rx = re.compile(rule)

    def predicate(output: str) -> bool:
        current = None
        for line in output.splitlines():
            table = re.search(r"\btable (\S+) \S+", line)
            if table:
                current = table.group(1)
            elif current == family and rule_rx.search(line):
                return True
        return False

    return Check(f"{description} (nftables {family} table)", predicate)


def _gateway_probe(node: str, purpose: str, expect: Check, *, evidence: bool):
    """Ping the node's default gateway, then show its neighbor entry."""
    return shell(
        node,
        f"{_GW}; echo gateway=$gw; ping -c 2 -W 1 $gw 2>&1 | tail -2; "
        "ip neigh show $gw",
        purpose,
        expect,
        evidence=evidence,
        timeout=20,
    )


def _probe_path(problem: ProblemBase):
    topo_size = getattr(problem.net_env, "topo_size", None) or "s"
    return get_probe_path(problem.scenario_name or "", topo_size=topo_size)


def _tcp_still_works(problem: ProblemBase, host: str) -> OracleStep:
    """Evidence: non-ICMP traffic from *host* still crosses the network.

    Uses the scenario's HTTP service when it has one, else a TCP connection to
    the default gateway, where a refusal (RST) also proves reachability.
    """
    path = _probe_path(problem)
    if path is not None and path.http_url:
        return http_probe(
            host,
            path.http_url,
            f"Fetch the service URL from {host} over TCP.",
            contains(r"http_code:[1-5]\d\d", "HTTP over TCP still gets responses"),
            evidence=True,
            times=2,
        )
    return shell(
        host,
        f'{_GW}; timeout 3 bash -c "exec 3<>/dev/tcp/$gw/179" 2>&1 '
        "&& echo TCP_CONNECTED || echo TCP_RC=$?",
        f"Open a TCP connection from {host} to its gateway.",
        contains(
            r"TCP_CONNECTED|Connection refused",
            "the gateway answers TCP (connect or RST)",
        ),
        evidence=True,
    )


_FRR_PEER_RE = re.compile(
    r"^\S+\s+4\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\d+\s+\S+\s+(\S+)", re.MULTILINE
)


def _frr_bgp_all_down(output: str) -> bool:
    """``show bgp summary`` lists peers and none is Established."""
    states = _FRR_PEER_RE.findall(output)
    return bool(states) and not any(state.isdigit() for state in states)


def _srl_bgp_all_down(output: str) -> bool:
    """SR Linux ``show ... bgp neighbor`` lists peers and none is established."""
    states = []
    for line in output.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) > 6 and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", cells[2]):
            states.append(cells[6].lower())
    return bool(states) and "established" not in states


def _srl_cpm_drop_179(output: str) -> bool:
    """An SRL CPM IPv4 filter entry matches TCP port 179 and drops it."""
    entries: dict[str, set[str]] = {}
    for seq, rest in re.findall(
        r"acl-filter cpm type ipv4 entry (\d+) (.+)$", output, re.MULTILINE
    ):
        entries.setdefault(seq, set()).add(rest.strip())
    return any(
        any(re.search(r"match transport \S+ value 179$", r) for r in rules)
        and "action drop" in rules
        for rules in entries.values()
    )


def _host_acl_target(ctx) -> str:
    """Probe-source host whose own ICMP / HTTP traffic the ACL drops."""
    if ctx.scenario == "enterprise_branch":
        return prefer_named(
            ctx.host_pool, "br1_corp_pc", first(ctx.host_pool) or ctx.host0
        )
    if ctx.scenario == "k8s_lab":
        return prefer_named(ctx.host_pool, "client", ctx.host0)
    if ctx.scenario in {"p4_dc_fabric", "sdn_l3_clos"}:
        return prefer_named(ctx.host_pool, "client_1_1", ctx.host0)
    return ctx.host0


# ==================================================================
# Problem: BGP Access Policy Misconfiguration - ACL blocking BGP traffic
# ==================================================================


class BGPAclBlockParams(BaseModel):
    """Parameters for injecting a BGP ACL block fault."""

    host_name: str = Field(description="Target router host name.")


class BGPAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "bgp_acl_block"
    description = "BGP control-plane traffic is blocked by an ACL."
    TAGS: str = ["bgp"]
    native_router_operations = frozenset(
        {"bgp_established_peers", "bgp_transport_blocked", "block_bgp_transport"}
    )
    supported_backends = ("kathara", "containerlab")

    Params = BGPAclBlockParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        # Transit routers without BGP (enterprise ISP cores) have no session.
        require_node_capability(
            ctx, cls.root_cause_name, inject.get("host_name"), "bgp"
        )

    def root_cause_resources(self, params: BGPAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: BGPAclBlockParams):
        ops = native_fault_ops(self, params.host_name, "block_bgp_transport")
        if ops is not None:
            ops.block_bgp_transport()
            wait_until(lambda: not ops.bgp_established_peers(), 8)
            return
        match self.lab_backend:
            case "containerlab":
                self._srl_acl_entries = self.runtime.srl_add_bgp_acl_drop_179(
                    params.host_name
                )
            case "kathara":
                self.runtime.add_nft_drop_rule(
                    params.host_name, "tcp dport 179 drop", family="inet"
                )
                self.runtime.add_nft_drop_rule(
                    params.host_name, "tcp sport 179 drop", family="inet"
                )
                # Hard-reset every session so the ACL takes effect now instead
                # of after the 180 s hold timer; blocked TCP 179 keeps them down.
                self.runtime.exec(
                    params.host_name,
                    "vtysh -c 'clear ip bgp *' 2>/dev/null || true",
                )
                wait_until(
                    lambda: (
                        not frr_bgp_has_established_session(
                            self.runtime, params.host_name
                        )
                    ),
                    8,
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def verify_fault(self, params: BGPAclBlockParams) -> dict:
        """Verify nftables or SRL ACL blocks TCP port 179 (BGP)."""
        ops = native_fault_ops(self, params.host_name, "bgp_transport_blocked")
        if ops is not None:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=ops.bgp_transport_blocked(),
                details={"host": params.host_name, "profile": ops.profile},
            )
        match self.lab_backend:
            case "containerlab":
                entries = getattr(self, "_srl_acl_entries", {})
                verified = self.runtime.srl_bgp_acl_drop_179_present(
                    params.host_name, entries
                )
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "srl_acl": verified,
                        "srl_acl_entries": sorted(entries),
                    },
                )
            case "kathara":
                return _verify_nft_drop(
                    self, params.host_name, "tcp dport 179 drop", "tcp sport 179 drop"
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def oracle_playbook(self, params: BGPAclBlockParams) -> OraclePlaybook:
        host = params.host_name
        finding = (
            f"An ACL on {host} drops BGP's TCP port 179, so its BGP sessions "
            "cannot re-establish and the routes they carried are lost."
        )
        if self.lab_backend == "containerlab":
            return OraclePlaybook(
                finding=finding,
                steps=[
                    OracleStep(
                        f"Check BGP session state on {host}.",
                        "srl_exec_cli",
                        {
                            "device_name": host,
                            "command": "show network-instance default protocols bgp neighbor",
                        },
                        Check(
                            "every BGP peer is down (none established)",
                            _srl_bgp_all_down,
                        ),
                        evidence=True,
                        attempts=6,
                        interval_sec=10,
                    ),
                    OracleStep(
                        f"Inspect the CPM (control-plane) IPv4 ACL on {host}.",
                        "srl_exec_cli",
                        {
                            "device_name": host,
                            "command": "info flat from running acl acl-filter cpm type ipv4 "
                            '| grep -E "value 179|action"',
                        },
                        Check(
                            "a CPM filter entry drops TCP port 179",
                            _srl_cpm_drop_179,
                        ),
                        evidence=True,
                    ),
                ],
            )
        return OraclePlaybook(
            finding=finding,
            steps=[
                shell(
                    host,
                    vtysh("show bgp summary"),
                    f"Check BGP session state on {host}.",
                    Check(
                        "every BGP peer is down (no Established session)",
                        _frr_bgp_all_down,
                    ),
                    evidence=True,
                    attempts=6,
                    interval_sec=10,
                    timeout=20,
                ),
                shell(
                    host,
                    "ss -tn state all '( sport = :179 or dport = :179 )' | head -20",
                    f"Look at BGP TCP connections on {host}.",
                    lacks(r"^ESTAB", "no BGP TCP connection is established"),
                ),
                _nft_drops(
                    host,
                    all_of(
                        _nft_table_rule(
                            "inet",
                            r"tcp dport 179 drop",
                            "a rule drops TCP destination port 179",
                        ),
                        _nft_table_rule(
                            "inet",
                            r"tcp sport 179 drop",
                            "a rule drops TCP source port 179",
                        ),
                    ),
                    f"Inspect the packet filter on {host} for rules on BGP's TCP port 179.",
                ),
            ],
        )


# ==================================================================
# Problem: OSPF Access Policy Misconfiguration - ACL blocking OSPF traffic
# ==================================================================


class OSPFAclBlockParams(BaseModel):
    """Parameters for injecting an OSPF ACL block fault."""

    host_name: str = Field(description="Target router host name.")


class OSPFAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name = "ospf_acl_block"
    description = "OSPF control-plane traffic is blocked by an ACL."
    TAGS: str = ["ospf"]
    native_router_operations = frozenset({"block_ospf", "ospf_blocked"})
    supported_backends = ("kathara", "containerlab")

    Params = OSPFAclBlockParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.router0}

    def root_cause_resources(self, params: OSPFAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: OSPFAclBlockParams):
        ops = native_fault_ops(self, params.host_name, "block_ospf")
        if ops is not None:
            ops.block_ospf()
            return
        match self.lab_backend:
            case "containerlab":
                self._srl_acl_entries = self.runtime.srl_add_ospf_acl_drop(
                    params.host_name
                )
                # Neighbor clear is best-effort; the OSPF dead interval is 40s.
                if not wait_until(
                    lambda: (
                        not self.runtime.srl_ospf_neighbor_interfaces(params.host_name)
                    ),
                    40,
                ):
                    neighbors = self.runtime.srl_ospf_neighbor_interfaces(
                        params.host_name
                    )
                    raise RuntimeError(
                        f"OSPF ACL drop on {params.host_name} left Full "
                        f"adjacencies: {neighbors}"
                    )
            case "kathara":
                self.runtime.add_nft_drop_rule(
                    params.host_name, "ip protocol ospf drop", family="inet"
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def verify_fault(self, params: OSPFAclBlockParams) -> dict:
        """Verify the router's packet filter drops OSPF."""
        ops = native_fault_ops(self, params.host_name, "ospf_blocked")
        if ops is not None:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=ops.ospf_blocked(),
                details={"host": params.host_name, "profile": ops.profile},
            )
        match self.lab_backend:
            case "containerlab":
                entries = getattr(self, "_srl_acl_entries", [])
                neighbors = self.runtime.srl_ospf_neighbor_interfaces(params.host_name)
                verified = (
                    self.runtime.srl_ospf_acl_drop_present(params.host_name, entries)
                    and not neighbors
                )
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "srl_acl_entries": entries,
                        "ospf_full_neighbors": neighbors,
                    },
                )
            case "kathara":
                return _verify_nft_drop(self, params.host_name, "ip protocol ospf drop")
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def oracle_playbook(self, params: OSPFAclBlockParams) -> OraclePlaybook:
        host = params.host_name
        return OraclePlaybook(
            finding=(
                f"An ACL on {host} drops OSPF (IP protocol 89), so its OSPF "
                "adjacencies time out and routes through it are withdrawn."
            ),
            steps=[
                shell(
                    host,
                    vtysh("show ip ospf neighbor"),
                    f"Check OSPF adjacencies on {host}.",
                    all_of(
                        contains(r"Neighbor ID", "OSPF answers"),
                        lacks(r"\bFull/", "no OSPF neighbor is Full"),
                    ),
                    evidence=True,
                    attempts=8,
                    interval_sec=10,
                    timeout=20,
                ),
                shell(
                    host,
                    vtysh("show ip ospf interface")
                    + " | grep -E 'is up|Area|Hello|Neighbor Count' | head -40",
                    f"Confirm OSPF is still enabled on {host}'s interfaces with its configured area and timers.",
                    contains(r"Area \S+", "OSPF runs on the interfaces"),
                    timeout=20,
                ),
                _nft_drops(
                    host,
                    _nft_table_rule(
                        "inet", r"ip protocol (ospf|89) drop", "a rule drops OSPF"
                    ),
                    f"Inspect the packet filter on {host} for rules on OSPF.",
                ),
            ],
        )


# ==================================================================
# Problem: ARP Access Policy Misconfiguration - ACL blocking ARP traffic
# ==================================================================


class ARPAclBlockParams(BaseModel):
    """Parameters for injecting an ARP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class ARPAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "path_ping"
    symptom_class = "unreachable"
    root_cause_name = "arp_acl_block"
    description = "ARP is blocked by an ACL."
    TAGS: str = ["arp"]
    # k8s_lab / llmd_lab client images lack nftables and cannot apt-install offline.
    COMPATIBLE_COLUMNS = NON_K8S_HOST_COLUMNS | {"iosxr_simple_bgp"}

    Params = ARPAclBlockParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": arp_l2_endpoint_host(ctx.roles, fallback=ctx.host0)}

    def root_cause_resources(self, params: ARPAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: ARPAclBlockParams):
        self.runtime.add_nft_drop_rule(params.host_name, "drop", family="arp")
        # Clos/P4 startups install nud permanent GW neigh; flush all leaves those.
        self.runtime.exec(params.host_name, "ip neigh flush nud permanent")
        self.runtime.exec(params.host_name, "ip neigh flush all")

    def verify_fault(self, params: ARPAclBlockParams) -> dict:
        """Verify nftables has a rule blocking ARP traffic."""
        return _verify_nft_drop(self, params.host_name, "drop", family="arp")

    def oracle_playbook(self, params: ARPAclBlockParams) -> OraclePlaybook:
        host = params.host_name
        return OraclePlaybook(
            finding=(
                f"An ARP-family nftables rule on {host} drops all ARP, so it "
                "cannot resolve its gateway or neighbors."
            ),
            steps=[
                shell(
                    host,
                    "ip -br addr; ip -4 route",
                    f"Show addressing and routes on {host}.",
                ),
                _gateway_probe(
                    host,
                    f"Ping the default gateway from {host} and read its neighbor entry.",
                    all_of(
                        contains(
                            r"\b0 received|100% packet loss",
                            "the gateway does not answer",
                        ),
                        contains(
                            r"\b(FAILED|INCOMPLETE)\b",
                            "ARP resolution for the gateway fails (FAILED/INCOMPLETE)",
                        ),
                    ),
                    evidence=True,
                ),
                _nft_drops(
                    host,
                    _nft_table_rule(
                        "arp", r"^\d+:\s*drop$", "a rule drops every ARP frame"
                    ),
                    f"Inspect the packet filter on {host}, including the ARP family.",
                ),
            ],
        )


# ==================================================================
# Problem: ACL blocking ICMP traffic
# ==================================================================


class IcmpAclBlockParams(BaseModel):
    """Parameters for injecting an ICMP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class IcmpAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "path_ping"
    symptom_class = "unreachable"
    root_cause_name = "icmp_acl_block"
    description = "ICMP is blocked by an ACL."
    TAGS: str = ["icmp"]

    Params = IcmpAclBlockParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": _host_acl_target(ctx)}

    def root_cause_resources(self, params: IcmpAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: IcmpAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "ip protocol icmp drop", family="ip"
        )

    def verify_fault(self, params: IcmpAclBlockParams) -> dict:
        """Verify nftables has a rule blocking ICMP traffic."""
        return _verify_nft_drop(
            self, params.host_name, "ip protocol icmp drop", family="ip"
        )

    def oracle_playbook(self, params: IcmpAclBlockParams) -> OraclePlaybook:
        host = params.host_name
        return OraclePlaybook(
            finding=(
                f"An nftables rule on {host} drops ICMP, so its pings fail "
                "while ARP and other traffic still work."
            ),
            steps=[
                _gateway_probe(
                    host,
                    f"Ping the default gateway from {host} and read its neighbor entry.",
                    all_of(
                        contains(
                            r"\b0 received|100% packet loss",
                            "the gateway does not answer pings",
                        ),
                        contains(
                            r"lladdr [0-9a-f:]{17}", "the gateway's MAC is resolved"
                        ),
                        lacks(r"\b(FAILED|INCOMPLETE)\b", "ARP resolution succeeds"),
                    ),
                    evidence=True,
                ),
                _tcp_still_works(self, host),
                _nft_drops(
                    host,
                    _nft_table_rule(
                        "ip", r"ip protocol icmp drop", "a rule drops IP protocol ICMP"
                    ),
                    f"Inspect the packet filter on {host}.",
                ),
            ],
        )


# ==================================================================
# Problem: ACL blocking HTTP traffic
# ==================================================================


class HttpAclBlockParams(BaseModel):
    """Parameters for injecting an HTTP ACL block fault."""

    host_name: str = Field(description="Target host name.")


class HttpAclBlock(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "path_http"
    symptom_class = "unreachable"
    root_cause_name = "http_acl_block"
    description = "HTTP traffic is blocked by an ACL."
    TAGS: str = ["http", "pc"]

    Params = HttpAclBlockParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": _host_acl_target(ctx)}

    def root_cause_resources(self, params: HttpAclBlockParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: HttpAclBlockParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "tcp dport 80 drop", family="inet"
        )

    def verify_fault(self, params: HttpAclBlockParams) -> dict:
        """Verify nftables has a rule blocking HTTP (port 80) traffic."""
        return _verify_nft_drop(self, params.host_name, "tcp dport 80 drop")

    def oracle_playbook(self, params: HttpAclBlockParams) -> OraclePlaybook:
        host = params.host_name
        path = _probe_path(self)
        steps = []
        if path is not None and (path.icmp_ip or path.dst_ip):
            steps.append(
                ping_works(
                    host,
                    path.icmp_ip or path.dst_ip,
                    f"Check that {host} still reaches the service host by ICMP.",
                    evidence=True,
                )
            )
        if path is not None and path.http_url:
            steps.append(
                http_probe(
                    host,
                    path.http_url,
                    f"Fetch the service URL from {host}.",
                    contains(r"http_code:000", "HTTP requests get no response"),
                    evidence=True,
                    times=2,
                )
            )
        steps.append(
            _nft_drops(
                host,
                _nft_table_rule(
                    "inet", r"tcp dport 80 drop", "a rule drops TCP destination port 80"
                ),
                f"Inspect the packet filter on {host}.",
            )
        )
        return OraclePlaybook(
            finding=(
                f"An nftables rule on {host} drops TCP port 80, so its HTTP "
                "requests time out."
            ),
            steps=steps,
        )


# ==================================================================
# Problem: DNS listener port blocked
# ==================================================================


class DNSPortBlockedParams(BaseModel):
    """Parameters for injecting a DNS port blocked fault."""

    host_name: str = Field(description="Target DNS server host name.")


class DNSPortBlocked(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "dns_port_blocked"

    description = "DNS service port is blocked."
    TAGS: str = ["dns", "http"]

    Params = DNSPortBlockedParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": ctx.dns0}

    def root_cause_resources(self, params: DNSPortBlockedParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: DNSPortBlockedParams):
        self.runtime.add_nft_drop_rule(
            params.host_name, "tcp dport 53 drop", family="inet"
        )
        self.runtime.add_nft_drop_rule(
            params.host_name, "udp dport 53 drop", family="inet"
        )

    def verify_fault(self, params: DNSPortBlockedParams) -> dict:
        """Verify nftables has rules blocking DNS port 53."""
        return _verify_nft_drop(
            self, params.host_name, "tcp dport 53 drop", "udp dport 53 drop"
        )

    def oracle_playbook(self, params: DNSPortBlockedParams) -> OraclePlaybook:
        host = params.host_name
        steps = dns_client_steps(
            self.net_env,
            host,
            contains(
                r"timed out|no servers could be reached",
                "DNS queries time out without any reply",
            ),
            evidence=True,
        )
        steps += [
            process_running(
                host,
                "named",
                f"Check that the DNS daemon still runs on {host}.",
                evidence=True,
            ),
            shell(
                host,
                "ss -lnu | grep -E ':53\\b' | head -5 || true",
                f"Check that {host} still listens on UDP port 53.",
                contains(r":53\b", "a UDP :53 listener is present"),
                evidence=True,
            ),
            _nft_drops(
                host,
                all_of(
                    _nft_table_rule(
                        "inet", r"udp dport 53 drop", "a rule drops UDP port 53"
                    ),
                    _nft_table_rule(
                        "inet", r"tcp dport 53 drop", "a rule drops TCP port 53"
                    ),
                ),
                f"Inspect the packet filter on {host}.",
            ),
        ]
        return OraclePlaybook(
            finding=(
                f"The DNS daemon on {host} is running, but an nftables rule "
                "drops port 53, so clients' queries time out."
            ),
            steps=steps,
        )
