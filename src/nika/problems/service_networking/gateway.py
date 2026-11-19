"""Evidence-backed gateway and NAT resource failures."""

from __future__ import annotations

import ipaddress
import json
import re
import time

from pydantic import BaseModel, Field

from nika.net_env.utils.routeros.parse import (
    routeros_connection_address_match,
    routeros_terse_rows,
)
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.oracle import (
    Check,
    router_cli,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    shell,
)
from nika.problems.support.polling import wait_until
from nika.problems.support.recovery import FileSnapshot, IPv4Snapshot, checked_exec
from nika.problems.support.router_ops import native_fault_ops
from nika.traffic.workload import NAT_SESSIONS, VIP_KEEPALIVE_SESSIONS

# Gateway L4 agent configuration, retained only for injection and recovery.
_LB_CONFIG = "/etc/lb-agent/config.json"


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
        OracleStep(
            f"Read the LB management state on {gateway}.",
            "lb_get_state",
            {"gateway": gateway},
            all_of(status_check, config_check),
            evidence=True,
            attempts=6,
            interval_sec=5,
        )
    ]
    if peer:
        steps.append(
            OracleStep(
                f"Compare LB occupancy on {peer}.", "lb_get_state", {"gateway": peer}
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
    CONFUSABLE_WITH = ("snat_port_pool_exhaustion",)
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
            OracleStep(
                f"Check connection admission failures on {gateway}.",
                "lb_get_state",
                {"gateway": gateway},
                Check(
                    "the full connection table rejects new admissions",
                    lambda out: json.loads(out).get("insert_failures", 0) > 0,
                ),
                evidence=True,
                attempts=6,
                interval_sec=10,
            ),
            OracleStep(
                f"Read lb_conn_table on {gateway} through P4Runtime: it holds only the capped entries.",
                "p4rt_exec",
                {"args": f"read-table --switch {gateway} --table lb_conn_table"},
                Check(
                    f"lb_conn_table occupancy is at or above its configured limit {limit}",
                    lambda o: len(json.loads(o)["tables"]["lb_conn_table"]) >= limit,
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
        self._lb_config = FileSnapshot.capture(
            self.runtime, params.host_name, _LB_CONFIG
        )
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

    def recover_fault(self, params: LbConnectionStateExhaustionParams) -> dict:
        from nika.net_env.p4_dc_gateway.l4_lb import learner_status

        snapshot = getattr(self, "_lb_config", None)
        if snapshot is None or snapshot.content is None:
            return build_verify_result(
                self.root_cause_name, False, {"error": "no_recovery_snapshot"}
            )
        # Restore only the field owned by this failure.
        from nika.net_env.p4_dc_gateway.l4_lb import set_agent_config

        original = json.loads(snapshot.content)["max_connections"]
        set_agent_config(self.runtime, params.host_name, max_connections=original)
        restored = wait_until(
            lambda: (
                learner_status(self.runtime, params.host_name).get("max_connections")
                == original
            ),
            15,
        )
        return build_verify_result(
            self.root_cause_name,
            restored,
            {"host": params.host_name, "max_connections": original},
        )

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
    CONFUSABLE_WITH = ("nat_mapping_removed_without_drain",)
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
            OracleStep(
                f"Read pool update events on {gateway}.",
                "lb_get_state",
                {"gateway": gateway},
                Check(
                    "a pool update occurred while connections were pending",
                    lambda out: any(
                        event["pending"] > 0
                        for event in json.loads(out).get("update_events", [])
                    ),
                ),
                evidence=True,
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
        self._lb_config = FileSnapshot.capture(
            self.runtime, params.host_name, _LB_CONFIG
        )
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

    def recover_fault(self, params: LbPendingConnectionUpdateRaceParams) -> dict:
        from nika.net_env.p4_dc_gateway.l4_lb import learner_status

        snapshot = getattr(self, "_lb_config", None)
        if snapshot is None or snapshot.content is None:
            return build_verify_result(
                self.root_cause_name, False, {"error": "no_recovery_snapshot"}
            )
        # Restore only the field owned by this failure.
        from nika.net_env.p4_dc_gateway.l4_lb import set_agent_config

        original = json.loads(snapshot.content)["learning_delay_ms"]
        set_agent_config(self.runtime, params.host_name, learning_delay_ms=original)
        restored = wait_until(
            lambda: (
                learner_status(self.runtime, params.host_name).get("learning_delay_ms")
                == original
            ),
            15,
        )
        return build_verify_result(
            self.root_cause_name,
            restored,
            {"host": params.host_name, "learning_delay_ms": original},
        )

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
    CONFUSABLE_WITH = (
        "lb_connection_state_exhaustion",
        "nat_mapping_removed_without_drain",
    )
    description = "SNAT source-port pool is exhausted."
    symptom_desc = "The available SNAT source-port pool is exhausted for concurrent outbound connections."
    TAGS = ["vpn", "http", "nat"]
    native_router_operations = frozenset(
        {"add_source_nat", "source_nat_rules", "save_config", "restore_config"}
    )
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
        from nika.problems.support.nat_sessions import (
            start_connections,
            stop_connections,
        )

        edge, public = params.host_name, params.public_ip
        first, last = params.port_start, params.port_end
        size = last - first + 1
        service = self.net_env.nat_probe_server()
        workload = self.net_env.business_workload()
        if service is None or workload is None:
            raise ValueError("SNAT reference requires a declared public HTTP service")
        server, address = service
        process = next(
            p for p in workload.processes if p.node == server and p.app == "web_service"
        )
        args = list(process.args)
        port = int(args[args.index("--port") + 1]) if "--port" in args else 80
        candidates = [
            name
            for name, identity in self.net_env.machine_identities.items()
            if getattr(identity.role, "value", identity.role) in {"host", "server"}
            and "linux" in identity.capabilities
        ]
        prefix = ipaddress.ip_network(params.source_prefix)
        key = f"snat_connections:{edge}:{public}"

        def client(outputs):
            for name in candidates:
                for value in re.findall(
                    r"\binet\s+(\S+)", outputs.get(f"{key}:{name}", "")
                ):
                    if ipaddress.ip_interface(value).ip in prefix:
                        return name
            raise ValueError("No observed Linux host belongs to the SNAT source prefix")

        def connection_args(outputs):
            return {
                "host_name": client(outputs),
                "command": start_connections(address, port, size + 16),
                "timeout": 15,
            }

        def cleanup_args(outputs):
            return {
                "host_name": client(outputs),
                "command": stop_connections(outputs[key]),
                "timeout": 10,
            }

        address_steps = [
            shell(
                name,
                "ip -4 -o addr show scope global",
                f"Read {name} addresses to locate the SNAT client.",
                save_as=f"{key}:{name}",
            )
            for name in candidates
        ]
        ports_in_use = f"conntrack -L -p tcp --reply-dst {public}"
        # Project and deduplicate only pool ports inside RouterOS. Full connection
        # rows exceed the normal tool output budget before Python can count them.
        routeros_ports = (
            ':local used [:toarray ""]; '
            ":foreach row in=[/ip firewall connection print detail as-value where "
            f"protocol=tcp and ({routeros_connection_address_match('reply-dst-address', public)})] do={{"
            ':local address [:tostr ($row->"reply-dst-address")]; '
            ':local port ($row->"reply-dst-port"); '
            ':if ([:typeof $port]="nil") do={'
            ':set port [:pick $address ([:find $address ":"]+1) [:len $address]]; }; '
            ":set port [:tonum $port]; "
            ':if ([:typeof $port]="num") do={'
            f":if (($port>={first}) && ($port<={last})) do={{"
            ":set ($used->[:tostr $port]) true; }; }; }; "
            ":foreach port,value in=$used do={"
            f':put ("reply-dst-address={public}:" . $port); }}'
        )

        def pool_full(output: str) -> bool:
            ports = set()
            for summary in re.findall(
                rf"^REPLY_DST_PORTS {re.escape(public)} \[([0-9,-]+)\]$",
                output,
                re.MULTILINE,
            ):
                for item in summary.split(","):
                    bounds = item.split("-", 1)
                    lower, upper = int(bounds[0]), int(bounds[-1])
                    ports.update(range(max(first, lower), min(last, upper) + 1))
            ports.update(
                {
                    int(match[1])
                    for line in output.splitlines()
                    if (
                        match := re.search(
                            rf"dst={re.escape(public)} sport=\d+ dport=(\d+)", line
                        )
                    )
                    and first <= int(match[1]) <= last
                }
            )
            ports.update(
                int(value)
                for value in re.findall(
                    rf"reply-dst-address={re.escape(public)}:(\d+)", output
                )
                if first <= int(value) <= last
            )
            ports.update(
                int(port)
                for row in routeros_terse_rows(output)
                if row.get("reply-dst-address") == public
                and (port := row.get("reply-dst-port", "")).isdigit()
                and first <= int(port) <= last
            )
            ports.update(
                int(value)
                for value in re.findall(rf"=> {re.escape(public)}:(\d+)", output)
                if first <= int(value) <= last
            )
            return len(ports) >= 0.9 * size

        return OraclePlaybook(
            finding=(
                f"A source NAT rule on {edge} translates {params.source_prefix} to "
                f"{public} with only ports {first}-{last}, and concurrent "
                "diagnostic connections fill the pool, so excess outbound connections cannot be "
                "translated."
            ),
            steps=[
                router_cli(
                    self.net_env,
                    edge,
                    f"Read source NAT policy on {edge}.",
                    Check(
                        "the source prefix uses the restricted public port pool",
                        lambda out: (
                            bool(
                                re.search(
                                    rf"ip saddr {re.escape(params.source_prefix)} .*snat to {re.escape(public)}:{first}-{last}\b",
                                    out,
                                )
                            )
                            or any(
                                f"src-address={params.source_prefix}" in line
                                and f"to-addresses={public}" in line
                                and f"to-ports={first}-{last}" in line
                                and "action=src-nat" in line
                                for line in out.splitlines()
                            )
                        ),
                    ),
                    frr="nft -a list chain ip nat POSTROUTING",
                    routeros_command="/ip firewall nat print terse",
                    evidence=True,
                ),
                *address_steps,
                OracleStep(
                    "Hold bounded HTTP connections while measuring the translated port pool.",
                    "exec_shell",
                    connection_args,
                    Check(
                        "some excess connections fail while at most the pool size connect",
                        lambda out: (
                            0 < (_json_field(out, "connected") or 0) <= size
                            and (_json_field(out, "failed") or 0) > 0
                        ),
                    ),
                    evidence=True,
                    save_as=key,
                ),
                router_cli(
                    self.net_env,
                    edge,
                    f"Count the translated source ports in use on {public}.",
                    Check(
                        f"at least 90% of the {size} pool ports are mapped",
                        pool_full,
                    ),
                    frr=ports_in_use,
                    routeros_command=routeros_ports,
                    evidence=True,
                    attempts=3,
                    interval_sec=10,
                ),
                OracleStep(
                    "Close the owned diagnostic connections and remove their private files.",
                    "exec_shell",
                    cleanup_args,
                    Check(
                        "the owned connection diagnostic is removed",
                        lambda out: json.loads(out).get("stopped") is True,
                    ),
                ),
            ],
        )

    def inject_fault(self, params: SnatPortPoolExhaustionParams):
        if params.port_end < params.port_start:
            raise ValueError("port_end must be greater than or equal to port_start")
        ops = native_fault_ops(self, params.host_name, "add_source_nat")
        if ops is not None:
            self._snat_native_config = ops.save_config()
            self._snat_native_rules = ops.source_nat_rules()
            # Ahead of the healthy pool rule, like the higher-priority nft chain.
            ops.add_source_nat(
                params.source_prefix,
                params.public_ip,
                protocol="tcp",
                to_ports=f"{params.port_start}-{params.port_end}",
            )
            return
        self._snat_handles_before = set(
            re.findall(
                r"# handle (\d+)",
                checked_exec(
                    self.runtime,
                    params.host_name,
                    "nft -a list chain ip nat POSTROUTING",
                ),
            )
        )
        self._snat_owned_handles = None
        # A port-restricted rule placed ahead of the site's own SNAT rule in
        # the edge's POSTROUTING chain, as an operator edit would leave it; a
        # separate chain would exist only on this edge and stand out.
        try:
            self.runtime.exec(
                params.host_name,
                f"nft insert rule ip nat POSTROUTING ip protocol tcp "
                f"ip saddr {params.source_prefix} "
                f"snat to {params.public_ip}:{params.port_start}-{params.port_end}",
            )
        finally:
            chain = checked_exec(
                self.runtime, params.host_name, "nft -a list chain ip nat POSTROUTING"
            )
            self._snat_owned_handles = {
                match[1]
                for line in chain.splitlines()
                if (match := re.search(r"# handle (\d+)", line))
                and match[1] not in self._snat_handles_before
                and f"ip saddr {params.source_prefix} " in line
                and f"snat to {params.public_ip}:{params.port_start}-{params.port_end}"
                in line
            }

    def recover_fault(self, params: SnatPortPoolExhaustionParams) -> dict:
        host = params.host_name
        ops = native_fault_ops(self, host, "source_nat_rules")
        if ops is not None:
            if not hasattr(self, "_snat_native_config"):
                return build_verify_result(
                    self.root_cause_name, False, {"error": "no_recovery_snapshot"}
                )
            ops.restore_config(self._snat_native_config)
            restored = ops.source_nat_rules() == self._snat_native_rules
        else:
            owned = getattr(self, "_snat_owned_handles", None)
            if owned is None:
                return build_verify_result(
                    self.root_cause_name, False, {"error": "no_recovery_snapshot"}
                )
            wanted = f"snat to {params.public_ip}:{params.port_start}-{params.port_end}"
            chain = checked_exec(
                self.runtime, host, "nft -a list chain ip nat POSTROUTING"
            )
            for line in chain.splitlines():
                match = re.search(r"# handle (\d+)", line)
                if (
                    match
                    and match[1] in owned
                    and f"ip saddr {params.source_prefix} " in line
                    and wanted in line
                ):
                    checked_exec(
                        self.runtime,
                        host,
                        f"nft delete rule ip nat POSTROUTING handle {match[1]}",
                    )
            chain = checked_exec(
                self.runtime, host, "nft -a list chain ip nat POSTROUTING"
            )
            restored = not any(
                (m := re.search(r"# handle (\d+)", line))
                and m[1] in owned
                and f"ip saddr {params.source_prefix} " in line
                and wanted in line
                for line in chain.splitlines()
            )
        return build_verify_result(
            self.root_cause_name,
            bool(restored),
            {"host": host, "pool_restored": bool(restored)},
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
    CONFUSABLE_WITH = ("lb_pending_connection_update_race", "snat_port_pool_exhaustion")
    description = "NAT mapping is removed before active flows drain."
    symptom_desc = (
        "An active SNAT address is removed without draining its conntrack mappings."
    )
    TAGS = ["vpn", "http", "nat"]
    native_router_operations = frozenset(
        {
            "add_source_nat",
            "source_nat_rules",
            "save_config",
            "restore_config",
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
        from nika.net_env.utils.routeros.common import routeros_port
        from nika.problems.support.router_ops import native_router_profile

        edge, wan = params.host_name, params.wan_interface
        native_wan = (
            routeros_port(self.net_env, edge, wan)
            if native_router_profile(self.net_env, edge) == "routeros"
            else wan
        )
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
                router_cli(
                    self.net_env,
                    edge,
                    f"Read WAN addresses on {edge}:{wan}.",
                    Check(
                        f"{new} is present and {old} is absent",
                        lambda out: (
                            bool(re.search(rf"\b{re.escape(new)}/", out))
                            and not re.search(rf"\b{re.escape(old)}/", out)
                        ),
                    ),
                    frr=f"ip -4 -br addr show dev {wan}",
                    routeros_command=f"/ip address print terse where interface={native_wan}",
                    evidence=True,
                ),
                router_cli(
                    self.net_env,
                    edge,
                    "Read the current source NAT address.",
                    Check(
                        f"the source prefix translates to {new}",
                        lambda out: (
                            bool(
                                re.search(
                                    rf"ip saddr {re.escape(prefix)} .*snat to {re.escape(new)}\b",
                                    out,
                                )
                            )
                            or any(
                                f"src-address={prefix}" in line
                                and f"to-addresses={new}" in line
                                for line in out.splitlines()
                            )
                        ),
                    ),
                    frr="nft -a list chain ip nat POSTROUTING",
                    routeros_command="/ip firewall nat print terse",
                    evidence=True,
                ),
                router_cli(
                    self.net_env,
                    edge,
                    f"Read old NAT mappings on {old}.",
                    Check(
                        "no mapping remains on the old address",
                        lambda out: (
                            bool(re.search(r"0 flow entries have been shown", out))
                            or ("total-entries=0" in out)
                            or bool(re.search(r"^\s*0\s*$", out))
                        ),
                    ),
                    frr=f"conntrack -L -p tcp -s {prefix} --reply-dst {old}",
                    routeros_command=(
                        "/ip firewall connection print count-only where protocol=tcp and "
                        f"({routeros_connection_address_match('reply-dst-address', old)})"
                    ),
                    evidence=True,
                ),
                router_cli(
                    self.net_env,
                    edge,
                    f"Read new NAT mappings on {new}.",
                    Check(
                        "mappings exist on the new address",
                        lambda out: (
                            bool(
                                re.search(r"[1-9]\d* flow entries have been shown", out)
                            )
                            or bool(re.search(r"^\s*[1-9]\d*\s*$", out))
                        ),
                    ),
                    frr=f"conntrack -L -p tcp -s {prefix} --reply-dst {new}",
                    routeros_command=(
                        "/ip firewall connection print count-only where protocol=tcp and "
                        f"({routeros_connection_address_match('reply-dst-address', new)})"
                    ),
                    evidence=True,
                    attempts=3,
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
            self._nat_native_config = ops.save_config()
            self._nat_native_rules = ops.source_nat_rules()
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
        self._nat_addresses = IPv4Snapshot.capture(
            self.runtime, params.host_name, params.wan_interface
        )
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
        self._nat_original_rule = next(
            (handles[0], line.split("# handle", 1)[0].strip())
            for line in chain.splitlines()
            if (match := re.search(r"# handle (\d+)", line)) and match[1] == handles[0]
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

    def recover_fault(self, params: NatMappingRemovedWithoutDrainParams) -> dict:
        host = params.host_name
        ops = native_fault_ops(self, host, "source_nat_rules")
        if ops is not None:
            if not hasattr(self, "_nat_native_config"):
                return build_verify_result(
                    self.root_cause_name, False, {"error": "no_recovery_snapshot"}
                )
            ops.restore_config(self._nat_native_config)
            restored = (
                ops.source_nat_rules() == self._nat_native_rules
                and ops.interface_addresses(params.wan_interface)
                == self._before["addresses"]
            )
        else:
            original = getattr(self, "_nat_original_rule", None)
            if original is None:
                return build_verify_result(
                    self.root_cause_name, False, {"error": "no_recovery_snapshot"}
                )
            handle, rule = original
            checked_exec(
                self.runtime,
                host,
                f"nft replace rule ip nat POSTROUTING handle {handle} {rule}",
            )
            self._nat_addresses.restore(self.runtime, host)
            chain = self.runtime.exec(host, "nft -a list chain ip nat POSTROUTING")
            restored = any(
                line.split("# handle", 1)[0].strip() == rule
                for line in chain.splitlines()
            )
        return build_verify_result(
            self.root_cause_name,
            bool(restored),
            {
                "host": host,
                "configuration_restored": bool(restored),
                "session_recovery": "New connections can use the restored mapping. Deleted connection state cannot be restored.",
            },
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
