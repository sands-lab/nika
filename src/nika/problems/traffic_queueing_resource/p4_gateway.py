"""Queue and traffic-induced failures for the P4 gateway benchmark."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.forwarding_encapsulation_policy.p4_gateway import (
    read_runtime_config_value,
)
from nika.problems.rca import interface_resource
from nika.problems.support.benchmark_targets import (
    p4_gateway_port_target,
    p4_port_options,
)
from nika.problems.support.p4_oracle import table_read, bmv2_ports
from nika.problems.support.oracle import OraclePlaybook, OracleStep, contains
from nika.problems.support.p4_gateway import set_ecn_threshold


class P4EcnMarkingDisabledParams(BaseModel):
    host_name: str
    intf_name: str
    bmv2_port: int = Field(gt=0)
    threshold: int = Field(default=1024, gt=64)


class P4EcnMarkingDisabled(ProblemBase):
    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    symptom_probe = "custom"
    symptom_class = "gray"
    root_cause_name = "p4_ecn_marking_disabled"
    CONFUSABLE_WITH = ("link_capacity_bottleneck",)
    description = "An ECN threshold above the queue-depth cap disables marking."
    symptom_desc = "Congested traffic receives no CE marks on the affected egress."
    TAGS = ["p4_runtime", "ecn", "queue", "http"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    Params = P4EcnMarkingDisabledParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        ctx.rng.choice(ctx.net_env.model.services)  # keep the shared service draw
        params = p4_gateway_port_target(ctx, gateways_only=False)
        params["threshold"] = "1024"
        return params

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        return p4_port_options(ctx, base, gateways_only=False)

    def root_cause_resources(self, params: P4EcnMarkingDisabledParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def inject_fault(self, params: P4EcnMarkingDisabledParams):
        self._original_threshold = read_runtime_config_value(
            self.runtime, params.host_name, "ecn_config", params.bmv2_port
        )
        if self._original_threshold is None:
            raise RuntimeError("Cannot read the ECN threshold before injection")
        set_ecn_threshold(
            self.runtime, params.host_name, params.bmv2_port, params.threshold
        )

    def recover_fault(self, params: P4EcnMarkingDisabledParams) -> dict:
        original = getattr(self, "_original_threshold", None)
        if original is not None:
            set_ecn_threshold(
                self.runtime, params.host_name, params.bmv2_port, original
            )
        observed = read_runtime_config_value(
            self.runtime, params.host_name, "ecn_config", params.bmv2_port
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=original is not None and observed == original,
            details={"original_threshold": original, "observed_threshold": observed},
        )

    def verify_fault(self, params: P4EcnMarkingDisabledParams) -> dict:
        observed = read_runtime_config_value(
            self.runtime, params.host_name, "ecn_config", params.bmv2_port
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=observed == params.threshold,
            details={
                "interface": params.intf_name,
                "threshold": params.threshold,
                "observed_threshold": observed,
            },
        )

    def oracle_playbook(self, params: P4EcnMarkingDisabledParams) -> OraclePlaybook:
        switch, intf, port = params.host_name, params.intf_name, params.bmv2_port

        def raised(rows: list[dict]) -> bool:
            thresholds = {
                int(row["match"]["egress_port"]): int(row["params"]["threshold"])
                for row in rows
            }
            others = [t for p, t in thresholds.items() if p != port]
            return (
                thresholds.get(port) == params.threshold
                and bool(others)
                and max(others) < params.threshold
            )

        return OraclePlaybook(
            finding=(
                f"The ECN marking threshold of {switch}:{intf} (BMv2 port {port}) is "
                f"{params.threshold} packets, far above the other ports, so the "
                "queue depth cannot reach the threshold, disabling CE marking."
            ),
            steps=[
                OracleStep(
                    "Read the measured INT queue occupancy for this ECN egress.",
                    "prometheus_query",
                    {
                        "query": f'max_over_time(p4_int_queue_depth_packets{{switch="{switch}",egress_port="{port}"}}[2m])'
                    },
                ),
                bmv2_ports(
                    switch,
                    contains(
                        rf'"port":\s*{port},\s*"interface":\s*"{intf}"',
                        f"port {port} is {intf}",
                    ),
                ),
                table_read(
                    switch,
                    "ecn_config",
                    f"port {port} marks ECN only at depth {params.threshold}, "
                    "above every other port's threshold",
                    raised,
                    f"Read the per-port ECN marking thresholds on {switch}.",
                ),
            ],
        )
