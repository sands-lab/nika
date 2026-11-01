"""Evidence-backed gateway and NAT resource failures."""

from __future__ import annotations

import ipaddress
import json
import re
import time

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    shell,
)
from nika.problems.support.router_ops import native_fault_ops
from nika.traffic.workload import NAT_SESSIONS, VIP_KEEPALIVE_SESSIONS

# Gateway L4 agent files, as an operator finds them on the gateway.
_LB_STATUS = "/var/lib/lb-agent/status.json"
_LB_CONFIG = "/etc/lb-agent/config.json"
_LB_LOG = "/tmp/lb-agent.log"


def _json_field(output: str, key: str) -> float | None:
    """Numeric *key* of the first JSON object in *output*."""
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line).get(key)
        except (ValueError, AttributeError):
            continue
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _json_check(description: str, predicate) -> Check:
    """Check over the learner status JSON fields."""

    def check(output: str) -> bool:
        fields = {
            key: _json_field(output, key)
            for key in (
                "learning_delay_ms",
                "max_connections",
                "learned",
                "pending",
                "insert_failures",
                "last_learn_ms",
            )
        }
        try:
            return bool(predicate(fields))
        except TypeError:
            return False

    return Check(description, check)


def _peer_gateway(net_env, gateway: str) -> str | None:
    model = getattr(net_env, "model", None)
    gateways = list(getattr(model, "gateways", None) or [])
    return next((name for name in gateways if name != gateway), None)


def _public_service_node(net_env) -> str | None:
    """Node hosting the public web service the NAT-ed sessions reach."""
    server = net_env.nat_probe_server()
    return server[0] if server else None


def _lb_agent_steps(
    gateway: str, peer: str | None, *, status_check: Check, config_check: Check
) -> list[OracleStep]:
    steps = [
        shell(
            gateway,
            f"cat {_LB_STATUS}; echo",
            f"Read the L4 connection learner's live status on {gateway}.",
            status_check,
            evidence=True,
        ),
    ]
    if peer:
        steps.append(
            shell(
                peer,
                f"cat {_LB_STATUS}; echo",
                f"Compare with the learner on {peer}, a healthy gateway.",
            )
        )
    steps.append(
        shell(
            gateway,
            f"cat {_LB_CONFIG}",
            f"Read the learner configuration on {gateway}.",
            config_check,
            evidence=True,
        )
    )
    return steps


class LbConnectionStateExhaustionParams(BaseModel):
    host_name: str = Field(default="gateway_1")
    max_connections: int = Field(default=4, ge=1, le=255)


class LbConnectionStateExhaustion(ProblemBase):
    """Gateway L4 agent connection limit set below the site's live connections.

    ``max_connections=4`` is lab-scaled: a client site keeps about 8 to 10 VIP
    connections open, so the limit plays the role of an undersized production
    connection-tracking table against its normal load.
    """

    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "gray"
    root_cause_name = "lb_connection_state_exhaustion"
    description = "Load-balancer connection-state table is exhausted."
    symptom_desc = (
        "The gateway L4 agent's connection limit is below what its client site "
        "keeps open, so lb_conn_table stays full and new connections are not "
        "pinned; a routine DIP pool update moves them to another backend, which "
        "resets them."
    )
    TAGS = ["p4", "p4_runtime", "http", "l4_load_balancer"]
    Params = LbConnectionStateExhaustionParams
    SYMPTOM_WORKLOAD = VIP_KEEPALIVE_SESSIONS

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("host_name") or "",)

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        # Below the keep-alive sessions one client site holds open at once.
        return {"host_name": "gateway_1", "max_connections": "4"}

    def root_cause_resources(self, params: LbConnectionStateExhaustionParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(
        self, params: LbConnectionStateExhaustionParams
    ) -> OraclePlaybook:
        gateway, limit = params.host_name, params.max_connections
        steps = _lb_agent_steps(
            gateway,
            _peer_gateway(self.net_env, gateway),
            status_check=_json_check(
                f"the learner caps the table at {limit} and has rejected inserts for a full table",
                lambda f: f["max_connections"] == limit and f["insert_failures"] > 0,
            ),
            config_check=contains(
                rf'"max_connections":\s*{limit}\b',
                f"max_connections is set to {limit}",
            ),
        )
        steps += [
            shell(
                gateway,
                f"grep -E 'lb_conn_table full|backend reset|pool update' {_LB_LOG} "
                "| tail -15",
                f"Read the learner log on {gateway}: new connections are not learned "
                "because lb_conn_table is full.",
                contains(r"lb_conn_table full", "the log reports lb_conn_table full"),
                evidence=True,
                attempts=6,
                interval_sec=10,
            ),
            OracleStep(
                f"Read lb_conn_table on {gateway} through P4Runtime: it holds only the capped entries.",
                "p4rt_exec",
                {"args": f"read-table --switch {gateway} --table lb_conn_table"},
                Check(
                    f"lb_conn_table holds between 1 and {limit} entries",
                    lambda o: 1 <= o.count('"set_lb_dip"') <= limit,
                ),
                evidence=True,
            ),
        ]
        return OraclePlaybook(
            finding=(
                f"The L4 learner on {gateway} caps lb_conn_table at {limit} "
                "connections, below what its client site keeps open, so new "
                "connections are never pinned and a pool update resets them."
            ),
            steps=steps,
        )

    def inject_fault(self, params: LbConnectionStateExhaustionParams):
        from nika.net_env.p4_dc_gateway.l4_lb import set_agent_config

        # A connection limit sized for a much smaller site; the agent rereads
        # its configuration once a second and the site's own sessions fill it.
        set_agent_config(
            self.runtime, params.host_name, max_connections=params.max_connections
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.verify_fault(params)["verified"]:
                return
            time.sleep(1.0)

    def verify_fault(self, params: LbConnectionStateExhaustionParams) -> dict:
        from nika.net_env.p4_dc_gateway.l4_lb import learner_status

        status = learner_status(self.runtime, params.host_name)
        limit = status.get("max_connections")
        return build_verify_result(
            self.root_cause_name,
            limit == params.max_connections
            and status.get("learned", 0) >= limit
            and status.get("age_sec", 99) < 5,
            {"gateway": params.host_name, "learner": status},
        )


class LbPendingConnectionUpdateRaceParams(BaseModel):
    host_name: str = Field(default="gateway_1")
    learning_delay_ms: int = Field(default=8000, ge=1, le=60000)
    seed: int = Field(default=1)


class LbPendingConnectionUpdateRace(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "gray"
    root_cause_name = "lb_pending_connection_update_race"
    description = "Pending connection races an unsafe load-balancer pool update."
    symptom_desc = (
        "Connection learning lags, so connections opened shortly before a routine "
        "DIP pool update are still pending in ConnTable and move to the new pool."
    )
    TAGS = ["p4", "p4_runtime", "http", "l4_load_balancer"]
    Params = LbPendingConnectionUpdateRaceParams
    SYMPTOM_WORKLOAD = VIP_KEEPALIVE_SESSIONS

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("host_name") or "",)

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        return {
            "host_name": "gateway_1",
            "learning_delay_ms": "8000",
            "seed": str(ctx.seed),
        }

    def root_cause_resources(self, params: LbPendingConnectionUpdateRaceParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(
        self, params: LbPendingConnectionUpdateRaceParams
    ) -> OraclePlaybook:
        from nika.net_env.p4_dc_gateway.topology_model import (
            CONNECTION_LEARNING_DELAY_MS,
        )

        gateway, delay = params.host_name, params.learning_delay_ms
        # Learning that takes longer than a tenth of the configured delay
        # (and orders of magnitude above the healthy few ms) is lagging.
        lag_ms = max(10 * CONNECTION_LEARNING_DELAY_MS, delay // 10)
        steps = _lb_agent_steps(
            gateway,
            _peer_gateway(self.net_env, gateway),
            status_check=_json_check(
                f"learning lags by about {delay} ms while lb_conn_table has room",
                lambda f: (
                    f["learning_delay_ms"] == delay
                    and (f["last_learn_ms"] or 0) >= lag_ms
                    and f["learned"] < f["max_connections"]
                ),
            ),
            config_check=contains(
                rf'"learning_delay_ms":\s*{delay}\b',
                f"learning_delay_ms is set to {delay}",
            ),
        )
        steps.append(
            shell(
                gateway,
                f"grep -E 'pool update|backend reset|lb_conn_table full' {_LB_LOG} "
                "| tail -15",
                f"Read the learner log on {gateway}: pool updates run while "
                "connections are still pending.",
                contains(
                    r"WARNING pool update with \d+ connection\(s\) not yet in lb_conn_table",
                    "a pool update ran with connections not yet learned",
                ),
                attempts=8,
                interval_sec=15,
            )
        )
        return OraclePlaybook(
            finding=(
                f"The L4 learner on {gateway} pins new connections only {delay} ms "
                "after their SYN, so routine DIP pool updates catch connections "
                "still pending in lb_conn_table and move them to the new pool."
            ),
            steps=steps,
        )

    def inject_fault(self, params: LbPendingConnectionUpdateRaceParams):
        from nika.net_env.p4_dc_gateway.l4_lb import set_agent_config

        set_agent_config(
            self.runtime, params.host_name, learning_delay_ms=params.learning_delay_ms
        )
        # The agent rereads its configuration once a second.
        deadline = time.time() + 10
        while time.time() < deadline:
            if self.verify_fault(params)["verified"]:
                return
            time.sleep(0.5)

    def verify_fault(self, params: LbPendingConnectionUpdateRaceParams) -> dict:
        from nika.net_env.p4_dc_gateway.l4_lb import learner_status
        from nika.net_env.p4_dc_gateway.topology_model import (
            CONNECTION_LEARNING_DELAY_MS,
        )

        status = learner_status(self.runtime, params.host_name)
        delay = status.get("learning_delay_ms")
        return build_verify_result(
            self.root_cause_name,
            isinstance(delay, int)
            and delay == params.learning_delay_ms
            and delay > CONNECTION_LEARNING_DELAY_MS
            and status.get("age_sec", 99) < 5,
            {
                "gateway": params.host_name,
                "healthy_learning_delay_ms": CONNECTION_LEARNING_DELAY_MS,
                "learner": status,
            },
        )


class SnatPortPoolExhaustionParams(BaseModel):
    host_name: str = Field(default="br1_edge")
    source_prefix: str = Field(default="10.1.40.0/24")
    public_ip: str
    port_start: int = Field(default=40000, ge=1024, le=65535)
    port_end: int = Field(default=40063, ge=1024, le=65535)


class SnatPortPoolExhaustion(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "snat_port_pool_exhaustion"
    description = "SNAT source-port pool is exhausted."
    symptom_desc = "The available SNAT source-port pool is exhausted for concurrent outbound connections."
    TAGS = ["vpn", "http", "nat"]
    native_router_operations = frozenset({"add_source_nat", "source_nat_rules"})
    Params = SnatPortPoolExhaustionParams
    SYMPTOM_WORKLOAD = NAT_SESSIONS

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("host_name") or "", inject.get("source_prefix") or "")

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "enterprise_branch":
            return {"host_name": ctx.host0}
        return {
            "host_name": "br1_edge",
            "source_prefix": "10.1.40.0/24",
            "public_ip": "198.18.1.10",
            "port_start": "40000",
            "port_end": "40063",
        }

    def root_cause_resources(self, params: SnatPortPoolExhaustionParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(self, params: SnatPortPoolExhaustionParams) -> OraclePlaybook:
        edge, public = params.host_name, params.public_ip
        first, last = params.port_start, params.port_end
        size = last - first + 1
        ports_in_use = (
            f"conntrack -L -p tcp --reply-dst {public} 2>/dev/null "
            f"| grep -oE 'dst={re.escape(public)} sport=[0-9]+ dport=[0-9]+' "
            f"| sed 's/.*dport=//' | awk '$1>={first} && $1<={last}' | sort -u "
            "| wc -l | sed 's/^/ports_in_use /'"
        )

        def pool_full(output: str) -> bool:
            match = re.search(r"^ports_in_use (\d+)$", output, re.M)
            return bool(match) and int(match.group(1)) >= 0.9 * size

        return OraclePlaybook(
            finding=(
                f"A source NAT rule on {edge} translates {params.source_prefix} to "
                f"{public} with only ports {first}-{last}, and the site's concurrent "
                "sessions hold every port, so new outbound connections cannot be "
                "translated."
            ),
            steps=[
                shell(
                    edge,
                    "nft -a list chain ip nat POSTROUTING 2>&1 | head -40",
                    f"Read the source NAT rules on {edge}.",
                    contains(
                        rf"ip saddr {re.escape(params.source_prefix)} .*snat to "
                        rf"{re.escape(public)}:{first}-{last}\b",
                        f"{params.source_prefix} is translated to {public}:{first}-{last}",
                    ),
                    evidence=True,
                ),
                shell(
                    edge,
                    f"echo pool_size {size}; {ports_in_use}; "
                    "conntrack -C 2>/dev/null | sed 's/^/conntrack_entries /'",
                    f"Count the translated source ports in use on {public}.",
                    Check(
                        f"at least 90% of the {size} pool ports are mapped",
                        pool_full,
                    ),
                    evidence=True,
                    attempts=3,
                    interval_sec=10,
                ),
            ],
        )

    def inject_fault(self, params: SnatPortPoolExhaustionParams):
        if params.port_end < params.port_start:
            raise ValueError("port_end must be greater than or equal to port_start")
        ops = native_fault_ops(self, params.host_name, "add_source_nat")
        if ops is not None:
            # Ahead of the healthy pool rule, like the higher-priority nft chain.
            ops.add_source_nat(
                params.source_prefix,
                params.public_ip,
                protocol="tcp",
                to_ports=f"{params.port_start}-{params.port_end}",
            )
            return
        # A port-restricted rule placed ahead of the site's own SNAT rule in
        # the edge's POSTROUTING chain, as an operator edit would leave it; a
        # separate chain would exist only on this edge and stand out.
        self.runtime.exec(
            params.host_name,
            f"nft insert rule ip nat POSTROUTING ip protocol tcp "
            f"ip saddr {params.source_prefix} "
            f"snat to {params.public_ip}:{params.port_start}-{params.port_end}",
        )

    def verify_fault(self, params: SnatPortPoolExhaustionParams) -> dict:
        ops = native_fault_ops(self, params.host_name, "source_nat_rules")
        if ops is not None:
            ports = f"{params.port_start}-{params.port_end}"
            rules = ops.source_nat_rules()
            return build_verify_result(
                self.root_cause_name,
                any(
                    rule.source_prefix == params.source_prefix
                    and rule.to_address == params.public_ip
                    and rule.to_ports == ports
                    for rule in rules
                ),
                {"edge": params.host_name, "rules": [vars(rule) for rule in rules]},
            )
        rules = self.runtime.exec(params.host_name, "nft -a list table ip nat")
        wanted = f"{params.public_ip}:{params.port_start}-{params.port_end}"
        return build_verify_result(
            self.root_cause_name,
            wanted in rules,
            {"edge": params.host_name, "rules": rules},
        )


class NatMappingRemovedWithoutDrainParams(BaseModel):
    host_name: str = Field(default="br1_edge")
    source_prefix: str = Field(default="10.1.40.0/24")
    nat_ip_a: str
    nat_ip_b: str
    wan_interface: str = Field(
        description="Edge interface that owns the SNAT addresses."
    )


class NatMappingRemovedWithoutDrain(ProblemBase):
    failure_domain = FailureDomain.SERVICE_NETWORKING
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name = "nat_mapping_removed_without_drain"
    description = "NAT mapping is removed before active flows drain."
    symptom_desc = (
        "An active SNAT address is removed without draining its conntrack mappings."
    )
    TAGS = ["vpn", "http", "nat"]
    native_router_operations = frozenset(
        {
            "add_source_nat",
            "clear_nat_mappings",
            "interface_addresses",
            "nat_mapping_count",
            "remove_interface_address",
        }
    )
    Params = NatMappingRemovedWithoutDrainParams
    SYMPTOM_WORKLOAD = NAT_SESSIONS

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("host_name") or "", inject.get("source_prefix") or "")

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "enterprise_branch":
            return {"host_name": ctx.host0}
        return {
            "host_name": "br1_edge",
            "source_prefix": "10.1.40.0/24",
            "nat_ip_a": "198.18.1.10",
            "nat_ip_b": "198.18.1.11",
            "wan_interface": ctx.net_env.primary_wan_interface(),
        }

    def root_cause_resources(self, params: NatMappingRemovedWithoutDrainParams):
        return [node_resource(params.host_name)]

    def oracle_playbook(
        self, params: NatMappingRemovedWithoutDrainParams
    ) -> OraclePlaybook:
        edge, wan = params.host_name, params.wan_interface
        old, new = params.nat_ip_a, params.nat_ip_b
        prefix = params.source_prefix
        server = _public_service_node(self.net_env)
        return OraclePlaybook(
            finding=(
                f"{edge} renumbered {prefix}'s source NAT from {old} to {new} and "
                f"removed {old} from {wan} without draining, so the sessions "
                f"translated to {old} lost their mappings."
            ),
            steps=[
                shell(
                    edge,
                    f"ip -4 -br addr show dev {wan}",
                    f"Read the WAN addresses on {edge}:{wan}.",
                    Check(
                        f"{new} is configured and {old} is gone",
                        lambda o: (
                            bool(re.search(rf"\b{re.escape(new)}/", o))
                            and not re.search(rf"\b{re.escape(old)}/", o)
                        ),
                    ),
                    evidence=True,
                ),
                shell(
                    edge,
                    "nft -a list chain ip nat POSTROUTING 2>&1 | head -40",
                    f"Read the source NAT rules on {edge}.",
                    contains(
                        rf"ip saddr {re.escape(prefix)} .*snat to {re.escape(new)}\b",
                        f"{prefix} now translates to {new}",
                    ),
                    evidence=True,
                ),
                shell(
                    edge,
                    # conntrack reports its count on stderr, so a missing tool
                    # or a rejected filter cannot read as "0 mappings".
                    f"echo mappings_on_old $(conntrack -L -p tcp -s {prefix} "
                    f"--reply-dst {old} 2>&1 >/dev/null); "
                    f"echo mappings_on_new $(conntrack -L -p tcp -s {prefix} "
                    f"--reply-dst {new} 2>&1 >/dev/null)",
                    f"Inspect {prefix}'s NAT mappings: they moved to {new} and none "
                    f"remain on {old}.",
                    all_of(
                        contains(
                            r"^mappings_on_old conntrack \S+ \(conntrack-tools\): "
                            r"0 flow entries have been shown\.$",
                            f"conntrack lists no mapping on {old}",
                        ),
                        contains(
                            r"^mappings_on_new conntrack \S+ \(conntrack-tools\): "
                            r"[1-9]\d* flow entries have been shown\.$",
                            f"conntrack lists the prefix's mappings on {new}",
                        ),
                    ),
                    evidence=True,
                ),
                *(
                    [
                        shell(
                            server,
                            "echo sessions_from_old $(ss -Htn state established "
                            f"'( dst {old} )' | wc -l); ss -Htn state established "
                            "| awk '{print $4}' | sed 's/:[0-9]*$//' | sort | uniq -c",
                            f"On the public service {server}, count sessions still "
                            f"open toward {old}.",
                            Check(
                                f"the server still holds sessions from {old}, which "
                                "were cut rather than drained",
                                lambda o: (
                                    bool(
                                        m := re.search(
                                            r"^sessions_from_old (\d+)$", o, re.M
                                        )
                                    )
                                    and int(m.group(1)) > 0
                                ),
                            ),
                            evidence=True,
                        )
                    ]
                    if server
                    else []
                ),
            ],
        )

    def inject_fault(self, params: NatMappingRemovedWithoutDrainParams):
        ops = native_fault_ops(self, params.host_name, "clear_nat_mappings")
        if ops is not None:
            wan, active = params.wan_interface, params.nat_ip_a
            self._before = {
                "addresses": ops.interface_addresses(wan),
                "mappings": ops.nat_mapping_count(active),
            }
            ops.add_source_nat(params.source_prefix, params.nat_ip_b, protocol="tcp")
            ops.remove_interface_address(wan, f"{active}/32")
            # Withdraw without draining: drop the mappings that used it.
            ops.clear_nat_mappings(active)
            return
        self._before = self.runtime.exec(
            params.host_name,
            f"ip -4 addr show dev {params.wan_interface}; "
            f"conntrack -L -p tcp --reply-dst {params.nat_ip_a} -o extended 2>/dev/null || true",
        )
        # Renumber the prefix's own SNAT rule to the second address, then
        # withdraw the first address without draining its mappings.
        chain = self.runtime.exec(
            params.host_name, "nft -a list chain ip nat POSTROUTING"
        )
        handles = [
            match.group(1)
            for line in chain.splitlines()
            if f"ip saddr {params.source_prefix} " in line
            and f"snat to {params.nat_ip_a}" in line
            and (match := re.search(r"# handle (\d+)", line))
        ]
        if not handles:
            raise RuntimeError(
                f"no SNAT rule for {params.source_prefix} to {params.nat_ip_a} "
                f"on {params.host_name}"
            )
        self.runtime.exec(
            params.host_name,
            f"nft replace rule ip nat POSTROUTING handle {handles[0]} "
            f"ip saddr {params.source_prefix} snat to {params.nat_ip_b}; "
            f"ip addr del {params.nat_ip_a}/32 dev {params.wan_interface}",
        )
        self.runtime.exec(
            params.host_name,
            f"conntrack -D -p tcp --reply-dst {params.nat_ip_a} 2>/dev/null || true",
        )
        # Packets already in flight can otherwise create an UNREPLIED reverse
        # tuple after the address disappears.  It is not an active SNAT
        # mapping, but removing it keeps the tracking state consistent with an
        # immediate, non-draining address withdrawal.
        self.runtime.exec(
            params.host_name,
            f"conntrack -D -p tcp --orig-dst {params.nat_ip_a} 2>/dev/null || true",
        )

    def verify_fault(self, params: NatMappingRemovedWithoutDrainParams) -> dict:
        ops = native_fault_ops(self, params.host_name, "nat_mapping_count")
        if ops is not None:
            addresses = ops.interface_addresses(params.wan_interface)
            mappings = ops.nat_mapping_count(params.nat_ip_a)
            return build_verify_result(
                self.root_cause_name,
                f"{params.nat_ip_a}/32" not in addresses
                and f"{params.nat_ip_b}/32" in addresses
                and mappings == 0,
                {
                    "edge": params.host_name,
                    "before": getattr(self, "_before", ""),
                    "addresses": addresses,
                    "conntrack_mappings": mappings,
                },
            )
        addresses = self.runtime.exec(
            params.host_name, f"ip -4 addr show dev {params.wan_interface}"
        )
        entries = self.runtime.exec(
            params.host_name,
            f"conntrack -L -p tcp --reply-dst {params.nat_ip_a} -o extended 2>/dev/null || true",
        )
        # Other LANs may still translate to NAT_IP_A; only the moved prefix counts.
        inside = ipaddress.ip_network(params.source_prefix, strict=False)
        moved = [
            line
            for line in entries.splitlines()
            if (match := re.search(r"\bsrc=(\S+)", line))
            and ipaddress.ip_address(match.group(1)) in inside
        ]
        return build_verify_result(
            self.root_cause_name,
            params.nat_ip_a not in addresses
            and params.nat_ip_b in addresses
            and not moved,
            {
                "edge": params.host_name,
                "before": getattr(self, "_before", ""),
                "addresses": addresses,
                "conntrack": entries,
            },
        )
