"""TCP SYN flood failure for the P4 gateway benchmark."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource


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
        command = (
            f"for source_port in $(seq {base_port} {base_port + params.flows - 1}); do "
            f"timeout {params.duration}s hping3 -S -s $source_port "
            f"-p {params.target_port} -i u{per_flow_interval_us} "
            f"{params.target_ip} >/dev/null 2>&1 & done"
        )
        self.runtime.exec(params.attacker_device, command, timeout=10)
        self._profile = {**params.model_dump(), "interval_us": interval_us}

    def verify_fault(self, params: TcpSynFloodAttackParams) -> dict:
        """Verify SYN generators toward the target are running on the attacker."""
        # Bracket the first character so pgrep does not match its own shell.
        pattern = f"[h]ping3 -S .*-p {params.target_port} .*{params.target_ip}"
        output = self.runtime.exec(
            params.attacker_device,
            f"pgrep -fc '{pattern}' 2>/dev/null || true",
            timeout=10,
        ).strip()
        try:
            flows_running = int(output.splitlines()[-1])
        except (IndexError, ValueError):
            flows_running = 0
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=flows_running >= 1,
            details={
                "flows_running": flows_running,
                "traffic_profile": getattr(self, "_profile", params.model_dump()),
            },
        )
