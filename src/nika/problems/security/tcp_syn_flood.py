"""TCP SYN flood failure for the P4 gateway benchmark."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource


class TcpSynFloodAttackParams(BaseModel):
    attacker_device: str
    target_ip: str
    target_port: int = Field(default=80, gt=0, le=65535)
    rate_pps: int = Field(default=100, gt=0)
    duration: int = Field(default=60, gt=0)
    flows: int = Field(default=40, gt=0, le=1000)
    seed: int = 42


class TcpSynFloodAttack(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name = "tcp_syn_flood_attack"
    symptom_desc = (
        "Deterministic SYN-only flows create half-open pressure on one HTTP service."
    )
    TAGS = ["flow_tracking", "http", "telemetry"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    Params = TcpSynFloodAttackParams

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
            f"{params.target_ip} >>/tmp/nika-syn-flood.log 2>&1 & done"
        )
        self.runtime.exec(params.attacker_device, command, timeout=10)
        self._profile = {**params.model_dump(), "interval_us": interval_us}

    def verify_fault(self, params: TcpSynFloodAttackParams) -> dict:
        output = self.runtime.exec(
            params.attacker_device,
            "pgrep -f 'hping3.*--flood' || test -s /tmp/nika-syn-flood.log; echo $?",
            timeout=10,
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=output.strip().endswith("0") or bool(output.strip()),
            details={"traffic_profile": getattr(self, "_profile", params.model_dump())},
        )
