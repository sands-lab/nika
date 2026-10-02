"""Forwarding and INT failures for the P4 gateway benchmark."""

from __future__ import annotations

import json
import shlex

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import interface_resource, node_resource
from nika.problems.support.benchmark_targets import (
    p4_gateway_port_target,
    p4_port_options,
    resolve_path_mtu_target,
)
from nika.problems.support.compatible_columns import LINUX_PMTU_COLUMNS
from nika.problems.support.p4_gateway import (
    set_icmp_frag_needed_filter,
    set_int_mtu,
    set_silent_destination_drop,
)
from nika.runtime.base import LabRuntime


def read_runtime_config_value(
    runtime: LabRuntime, switch: str, table: str, port: int
) -> int | None:
    """Read the live action parameter of ``table``'s entry for ``port`` via P4Runtime."""
    output = runtime.exec(
        "fabric_mgr",
        f"python3 /opt/nika/p4rt_manager.py read --switch {shlex.quote(switch)}",
        timeout=30,
    )
    try:
        observed = json.loads(output)["switches"][switch]
    except (ValueError, KeyError, TypeError):
        return None
    for row in (observed.get("runtime_config") or {}).get(table) or []:
        if row.get("is_default") or int(row.get("key", -1)) != int(port):
            continue
        values = [v for k, v in row.items() if k.startswith("param_")]
        if values:
            return int(values[0])
    return None


# nftables match for ICMP Destination Unreachable / Fragmentation Needed.
# Written the way ``nft list`` prints it (nft adds and hides the implied
# ``ip protocol icmp`` dependency), so verify_fault can match it exactly.
_FRAG_NEEDED_NFT_RULE = "icmp type destination-unreachable icmp code frag-needed drop"


class IcmpFragNeededFilterMisconfigurationParams(BaseModel):
    host_name: str = Field(
        default="gateway_1",
        description="Node that drops ICMP Fragmentation Needed (P4 gateway or Linux router).",
    )


class IcmpFragNeededFilterMisconfiguration(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "icmp_frag_needed_filter_misconfiguration"
    description = "ICMP Fragmentation Needed messages are filtered."
    symptom_desc = (
        "ICMP Fragmentation Needed is filtered, so PMTUD cannot shrink the path "
        "MTU and large transfers stall while small packets still work."
    )
    TAGS = ["icmp"]
    COMPATIBLE_COLUMNS = LINUX_PMTU_COLUMNS | {"p4_dc_gateway"}
    Params = IcmpFragNeededFilterMisconfigurationParams
    BENCHMARK_TARGETS = "canonical"
    BENCHMARK_COORDINATES = frozenset({"mtu_mismatch"})

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario == "p4_dc_gateway":
            return {"host_name": "gateway_1"}
        # Linux / FRR path: drop Frag Needed on an intermediate router.
        mtu_target = resolve_path_mtu_target(
            ctx.scenario, ctx.net_env, ctx.rng, list(ctx.router_pool), ctx.backend
        )
        return {"host_name": mtu_target["host_name"]}

    @classmethod
    def coordinate_benchmark_inject(cls, ctx, params_by_problem):
        # PMTUD black hole: filter Frag Needed on the router that lowered the MTU.
        mtu = dict(params_by_problem["mtu_mismatch"])
        frag = dict(params_by_problem["icmp_frag_needed_filter_misconfiguration"])
        frag["host_name"] = mtu["host_name"]
        params_by_problem["icmp_frag_needed_filter_misconfiguration"] = frag
        return params_by_problem

    def root_cause_resources(self, params: IcmpFragNeededFilterMisconfigurationParams):
        return [node_resource(params.host_name)]

    def _uses_p4_filter(
        self, params: IcmpFragNeededFilterMisconfigurationParams
    ) -> bool:
        name = params.host_name
        if name.startswith("gateway_"):
            return True
        scenario = getattr(self, "scenario_name", None) or ""
        return scenario == "p4_dc_gateway"

    def inject_fault(self, params: IcmpFragNeededFilterMisconfigurationParams):
        if self._uses_p4_filter(params):
            self._result = set_icmp_frag_needed_filter(self.runtime, params.host_name)
            return
        self.runtime.add_nft_drop_rule(
            params.host_name, _FRAG_NEEDED_NFT_RULE, family="ip"
        )
        self._result = "linux-nft-frag-needed"

    def verify_fault(self, params: IcmpFragNeededFilterMisconfigurationParams) -> dict:
        if self._uses_p4_filter(params):
            # The P4Runtime read API does not list this ACL; trust the write ack.
            output = getattr(self, "_result", "")
            return build_verify_result(
                self.root_cause_name,
                "icmp-frag-needed" in output,
                {"gateway": params.host_name, "output": output[:200]},
            )
        verified = self.runtime.nft_drop_rule_present(
            params.host_name, _FRAG_NEEDED_NFT_RULE, family="ip"
        )
        return build_verify_result(
            self.root_cause_name,
            verified,
            {"host": params.host_name, "nft_rule": _FRAG_NEEDED_NFT_RULE},
        )


class P4TcamEntryCorruptionParams(BaseModel):
    host_name: str = Field(description="Gateway or spine with the silent drop hook.")
    target_ip: str = Field(
        description="Destination address affected by the corrupt lookup."
    )
    control_source: str = Field(description="Client used for the healthy control flow.")


class P4TcamEntryCorruption(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "p4_tcam_entry_corruption"
    description = "A forwarding/TCAM entry is silently corrupted for one flow."
    symptom_desc = "One destination flow stops after a switch whose P4Runtime state remains healthy."
    TAGS = ["p4_runtime", "telemetry", "flow_tracking"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    Params = P4TcamEntryCorruptionParams
    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        rng = ctx.rng
        model = ctx.net_env.model
        service = rng.choice(model.services)
        target = rng.choice(model.gateways + model.spines)
        control = (
            next(
                client.name
                for client in model.clients
                if client.name != model.clients[0].name
            )
            if len(model.clients) > 1
            else model.clients[0].name
        )
        return {"host_name": target, "target_ip": service.ip, "control_source": control}

    def root_cause_resources(self, params: P4TcamEntryCorruptionParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: P4TcamEntryCorruptionParams):
        self._result = set_silent_destination_drop(
            self.runtime, params.host_name, params.target_ip
        )

    def verify_fault(self, params: P4TcamEntryCorruptionParams) -> dict:
        output = getattr(self, "_result", "")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified="Error" not in output,
            details={
                "switch": params.host_name,
                "target_ip": params.target_ip,
                "hook_programmed": "Error" not in output,
            },
        )


class IntInsufficientMtuHeadroomParams(BaseModel):
    host_name: str = Field(description="INT source gateway.")
    intf_name: str = Field(description="Gateway egress interface.")
    bmv2_port: int = Field(gt=0)
    int_mtu: int = Field(default=1480, ge=576, le=1500)


class IntInsufficientMtuHeadroom(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "int_insufficient_mtu_headroom"
    root_cause_owner = "interface"
    description = "INT encapsulation lacks sufficient MTU headroom."
    symptom_desc = "Near-MTU watched packets cannot carry the fixed INT-MX header."
    TAGS = ["p4_runtime", "int", "telemetry", "http"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    Params = IntInsufficientMtuHeadroomParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        ctx.rng.choice(ctx.net_env.model.services)  # keep the shared service draw
        params = p4_gateway_port_target(ctx, gateways_only=True)
        params["int_mtu"] = "1480"
        return params

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        return p4_port_options(ctx, base, gateways_only=True)

    def root_cause_resources(self, params: IntInsufficientMtuHeadroomParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def inject_fault(self, params: IntInsufficientMtuHeadroomParams):
        set_int_mtu(self.runtime, params.host_name, params.bmv2_port, params.int_mtu)

    def verify_fault(self, params: IntInsufficientMtuHeadroomParams) -> dict:
        observed = read_runtime_config_value(
            self.runtime, params.host_name, "int_mtu_config", params.bmv2_port
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=observed == params.int_mtu,
            details={
                "interface": params.intf_name,
                "int_mtu": params.int_mtu,
                "observed_int_mtu": observed,
            },
        )
