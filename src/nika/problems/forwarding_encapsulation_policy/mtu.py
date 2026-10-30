from __future__ import annotations

import re

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca.inventory import interface_on
from nika.problems.support.benchmark_targets import resolve_path_mtu_target
from nika.problems.support.compatible_columns import (
    LINUX_PMTU_COLUMNS,
    LINUX_PMTU_PLATFORMS,
)
from nika.problems.support.router_ops import native_fault_ops
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

# ==========================================
# Problem: MTU mismatch (one interface below its neighbors)
# ==========================================
#
# Lowers the MTU of one interface only, so it no longer matches the far end
# of its link. OSPF stops at ExStart/Exchange because the Database
# Description MTU check fails (RFC 2328 10.6), routed DF packets larger than
# the MTU that leave through it elicit ICMP Fragmentation Needed, and large
# transfers across it stall.
# Legacy id: link_fragmentation_disabled.


_MTU_RE = re.compile(r"\bmtu\s+(\d+)\b")


class MtuMismatchParams(BaseModel):
    """Parameters for injecting a path-MTU / MTU-mismatch fault."""

    host_name: str = Field(description="Intermediate forwarding node.")
    intf_name: str = Field(description="Interface whose MTU is misconfigured.")
    mtu: int = Field(
        default=500,
        ge=68,
        le=1500,
        description="Misconfigured interface MTU in bytes.",
    )


class MtuMismatch(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "mtu_mismatch"
    root_cause_owner = "interface"
    description = "Interface MTU is misconfigured and mismatches its link peer."
    TAGS: list[str] = ["link", "icmp"]
    native_router_operations = frozenset({"interface_mtu", "set_interface_mtu"})
    COMPATIBLE_COLUMNS = LINUX_PMTU_COLUMNS
    COMPATIBLE_PLATFORMS = LINUX_PMTU_PLATFORMS

    Params = MtuMismatchParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return resolve_path_mtu_target(
            ctx.scenario, ctx.net_env, ctx.rng, list(ctx.router_pool), ctx.backend
        )

    symptom_desc = (
        "Users report size-dependent connectivity: small packets succeed while "
        "large packets across the link are dropped or report ICMP "
        "Fragmentation Needed, and OSPF adjacencies over it may not form."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.mtu = 500

    def root_cause_resources(self, params: MtuMismatchParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: MtuMismatchParams):
        match self.lab_backend:
            case "kathara" | "containerlab":
                self._inject_mtu_mismatch(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _set_mtu(self, host: str, intf: str, mtu: int) -> None:
        ops = native_fault_ops(self, host, "set_interface_mtu")
        if ops is not None:
            ops.set_interface_mtu(intf, mtu)
            return
        self.runtime.exec(host, f"ip link set dev {intf} mtu {int(mtu)}")

    def _inject_mtu_mismatch(self, params: MtuMismatchParams) -> None:
        self.mtu = params.mtu
        self._set_mtu(params.host_name, params.intf_name, params.mtu)
        system_logger.info(
            f"Injected MTU mismatch on {params.host_name}:{params.intf_name} "
            f"(mtu={params.mtu})"
        )

    def verify_fault(self, params: MtuMismatchParams) -> dict:
        """Verify the interface MTU was lowered."""
        match self.lab_backend:
            case "kathara" | "containerlab":
                return self._verify_mtu_mismatch(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _read_mtu(self, host: str, intf: str) -> int | None:
        ops = native_fault_ops(self, host, "interface_mtu")
        if ops is not None:
            return ops.interface_mtu(intf)
        output = self.runtime.exec(
            host, f"ip -o link show dev {intf} 2>/dev/null || true"
        ).strip()
        match = _MTU_RE.search(output)
        if match is None:
            return None
        return int(match.group(1))

    def _verify_mtu_mismatch(self, params: MtuMismatchParams) -> dict:
        observed = self._read_mtu(params.host_name, params.intf_name)
        verified = observed == int(params.mtu)
        details: dict = {
            "host": params.host_name,
            "intf": params.intf_name,
            "mtu": params.mtu,
            "observed_mtu": observed,
        }
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details=details,
        )
