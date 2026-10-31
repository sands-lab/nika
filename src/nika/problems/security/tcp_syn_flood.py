"""TCP SYN flood failure for the P4 gateway benchmark."""

from __future__ import annotations

import re
import shlex
import time

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.oracle import Check, OraclePlaybook, OracleStep, shell
from nika.problems.support.process_identity import NEUTRAL_PROBE_NAME, exec_as

_COLLECTOR_FRESH_SEC = 30.0


class TcpSynFloodAttackParams(BaseModel):
    attacker_device: str
    target_ip: str
    target_port: int = Field(default=80, gt=0, le=65535)
    rate_pps: int = Field(default=1000, gt=0)
    duration: int = Field(
        default=3600,
        gt=0,
        description="Legacy duration parameter; flood persists until recovery.",
    )
    flows: int = Field(default=100, gt=0, le=1000)
    seed: int = 42


class TcpSynFloodAttack(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    symptom_probe = "custom"
    symptom_class = "degradation"
    dynamic_artifact = True
    root_cause_name = "tcp_syn_flood_attack"
    description = "TCP SYN flood attack against a target service."
    symptom_desc = (
        "Deterministic SYN-only flows create half-open pressure on one HTTP service."
    )
    TAGS = ["flow_tracking", "http", "telemetry"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    Params = TcpSynFloodAttackParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        model = ctx.net_env.model
        service = ctx.rng.choice(model.services)
        return {
            "attacker_device": ctx.rng.choice(model.clients).name,
            "target_ip": service.ip,
            "target_port": "80",
            "rate_pps": "1000",
            "duration": "3600",
            "flows": "100",
            "seed": str(ctx.seed),
        }

    def root_cause_resources(self, params: TcpSynFloodAttackParams):
        return [node_resource(params.attacker_device)]

    def oracle_playbook(self, params: TcpSynFloodAttackParams) -> OraclePlaybook:
        model = getattr(self.net_env, "model", None)
        attacker, target, port = (
            params.attacker_device,
            params.target_ip,
            params.target_port,
        )
        attacker_ip = next(
            (c.ip for c in getattr(model, "clients", []) if c.name == attacker), None
        )
        server = next(
            (s.name for s in getattr(model, "services", []) if s.ip == target), None
        )
        # One SYN flow per source port: far more concurrent flows from one
        # client than its regular sessions open.
        min_flows = max(10, params.flows // 4)
        steps: list[OracleStep] = []
        if server:
            peers = (
                f"ss -Htn state syn-recv '( sport = :{port} )' | awk '{{print $4}}' "
                "| sed 's/:[0-9]*$//' | sort | uniq -c | sort -rn"
            )
            steps += [
                shell(
                    server,
                    f"echo half_open $(ss -Htn state syn-recv '( sport = :{port} )' "
                    f"| wc -l); {peers} | head -5; "
                    f"echo top $({peers} | head -1)",
                    f"Count half-open connections on {server} ({target}:{port}) per client.",
                    Check(
                        f"at least {min_flows} half-open connections"
                        + (f", mostly from {attacker_ip}" if attacker_ip else ""),
                        lambda o: (
                            bool(m := re.search(r"^top (\d+) (\S+)$", o, re.M))
                            and int(m.group(1)) >= min_flows
                            and (attacker_ip is None or m.group(2) == attacker_ip)
                        ),
                    ),
                    evidence=True,
                    attempts=2,
                ),
                shell(
                    server,
                    f"timeout 2 tcpdump -nl -c 3000 'tcp dst port {port} and "
                    "(tcp[tcpflags] & (tcp-syn|tcp-ack) == tcp-syn)' 2>/dev/null "
                    '| awk \'{for (i = 1; i < NF; i++) if ($i == "IP") '
                    '{sub(/\\.[0-9]+$/, "", $(i+1)); print $(i+1); break}}\' '
                    "| sort | uniq -c | sort -rn | head -5 | sed 's/^ */syns /'",
                    f"Sample connection-opening SYNs arriving at {server} for two "
                    "seconds, per source.",
                    Check(
                        f"one source sends at least {min_flows} SYNs in two seconds"
                        + (f" ({attacker_ip})" if attacker_ip else ""),
                        lambda o: (
                            bool(m := re.search(r"^syns (\d+) (\S+)$", o, re.M))
                            and int(m.group(1)) >= min_flows
                            and (attacker_ip is None or m.group(2) == attacker_ip)
                        ),
                    ),
                    evidence=True,
                    timeout=15,
                ),
            ]

        steps += [
            OracleStep(
                f"Query INT telemetry for the latest packets from {attacker} "
                f"({attacker_ip}) to {target}:{port}, to trace the flood's source.",
                "int_query_telemetry",
                {
                    "start_time": "-30",
                    "dst": target,
                    "dst_port": port,
                    **({"src": attacker_ip} if attacker_ip else {}),
                    "limit": 2,
                },
            ),
            shell(
                attacker,
                "echo syn_senders $(ps -eo args | grep -cE "
                f"'^[^ ]+ -S .*-p {port} .*{re.escape(target)}$'); "
                f"ps -eo args | grep -E -- ' -S .*{re.escape(target)}$' | head -3",
                f"List the processes on {attacker} sending to {target}:{port}.",
                Check(
                    "processes send SYN-only packets to the service",
                    lambda o: (
                        bool(m := re.search(r"^syn_senders (\d+)$", o, re.M))
                        and int(m.group(1)) >= 1
                    ),
                ),
                evidence=True,
            ),
        ]
        return OraclePlaybook(
            finding=(
                f"{attacker} floods {target}:{port} with SYN-only packets across "
                f"{params.flows} source ports and never completes the handshakes, "
                "leaving half-open connections on the service."
            ),
            steps=steps,
        )

    def inject_fault(self, params: TcpSynFloodAttackParams):
        interval_us = max(1, 1_000_000 // params.rate_pps)
        per_flow_interval_us = max(1, round(1_000_000 * params.flows / params.rate_pps))
        base_port = 20000 + (params.seed % 20000)
        # Each flow's SYN generator runs the neutrally named `latency-probe`
        # hardlink (see process_identity.py), so `ps` shows a plausible
        # host-monitoring process in both argv and kernel comm, not the raw
        # flood tool.
        flow_cmd = exec_as(
            NEUTRAL_PROBE_NAME,
            f'{NEUTRAL_PROBE_NAME} -S -s "$1" -p {params.target_port} '
            f"-i u{per_flow_interval_us} {params.target_ip}",
        )
        command = (
            f"for source_port in $(seq {base_port} {base_port + params.flows - 1}); do "
            f"nohup bash -c {shlex.quote(flow_cmd)} "
            '_ "$source_port" </dev/null >/dev/null 2>&1 & echo $!; done'
        )
        output = self.runtime.exec(params.attacker_device, command, timeout=10)
        self._flow_pids = [
            int(pid) for pid in output.splitlines() if pid.strip().isdigit()
        ]
        self._profile = {**params.model_dump(), "interval_us": interval_us}

    def recover_fault(self, params: TcpSynFloodAttackParams) -> dict:
        for pid in getattr(self, "_flow_pids", []):
            self.runtime.exec(
                params.attacker_device,
                f"if ps -p {pid} -o args= | grep -q '{NEUTRAL_PROBE_NAME} -S '; then kill {pid}; fi",
                timeout=10,
            )
        state = self.verify_fault(params)["details"]
        return build_verify_result(
            self.root_cause_name, state["flows_running"] == 0, state
        )

    def verify_fault(self, params: TcpSynFloodAttackParams) -> dict:
        """Verify SYN flood processes and that INT collection is still live."""
        from nika.net_env.p4_dc_gateway.topology_model import INT_REPORTS

        # Bracket the first character so pgrep does not match its own shell.
        pattern = (
            f"[{NEUTRAL_PROBE_NAME[0]}]{NEUTRAL_PROBE_NAME[1:]} -S "
            f".*-p {params.target_port} .*{params.target_ip}"
        )
        output = self.runtime.exec(
            params.attacker_device,
            f"pgrep -fc '{pattern}' 2>/dev/null || true",
            timeout=10,
        ).strip()
        try:
            flows_running = int(output.splitlines()[-1])
        except (IndexError, ValueError):
            flows_running = 0
        collector_alive = (
            self.runtime.exec(
                "collector",
                "pgrep -f '[c]ollector.py' >/dev/null && echo yes || echo no",
                timeout=10,
            )
            .strip()
            .endswith("yes")
        )
        mtime_raw = self.runtime.exec(
            "collector",
            f"stat -c %Y {INT_REPORTS} 2>/dev/null || echo 0",
            timeout=10,
        ).strip()
        try:
            report_age_sec = max(0.0, time.time() - float(mtime_raw.splitlines()[-1]))
        except (IndexError, ValueError):
            report_age_sec = None
        collector_fresh = (
            collector_alive
            and report_age_sec is not None
            and report_age_sec <= _COLLECTOR_FRESH_SEC
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=flows_running >= 1 and collector_fresh,
            details={
                "flows_running": flows_running,
                "traffic_profile": getattr(self, "_profile", params.model_dump()),
                "collector_alive": collector_alive,
                "collector_fresh": collector_fresh,
                "last_report_age_sec": report_age_sec,
            },
        )
