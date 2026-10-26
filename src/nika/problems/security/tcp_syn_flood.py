"""TCP SYN flood failure for the P4 gateway benchmark."""

from __future__ import annotations

import shlex
import time

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.process_identity import NEUTRAL_PROBE_NAME, exec_as

_COLLECTOR_FRESH_SEC = 30.0


class TcpSynFloodAttackParams(BaseModel):
    attacker_device: str
    target_ip: str
    target_port: int = Field(default=80, gt=0, le=65535)
    rate_pps: int = Field(default=1000, gt=0)
    duration: int = Field(default=3600, gt=0)
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
            f"timeout {params.duration}s bash -c {shlex.quote(flow_cmd)} "
            '_ "$source_port" >/dev/null 2>&1 & done'
        )
        self.runtime.exec(params.attacker_device, command, timeout=10)
        self._profile = {**params.model_dump(), "interval_us": interval_us}

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
