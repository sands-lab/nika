from __future__ import annotations

import re

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca.inventory import (
    interface_on,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.problems.support.benchmark_targets import resolve_path_mtu_target
from nika.problems.support.compatible_columns import (
    LINUX_PMTU_COLUMNS,
    LINUX_PMTU_PLATFORMS,
)
from nika.problems.support.oracle import (
    interface_read,
    compare,
    Check,
    OraclePlaybook,
    is_ip_node,
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


def _segment_neighbor(net_env, node: str, intf: str) -> tuple[str, str] | None:
    """First other addressed endpoint on the link or LAN segment of ``node:intf``."""
    for _key, raw in iter_link_termination_points(net_env):
        ends = [parse_endpoint(str(item)) for item in raw]
        if (node, intf) in ends:
            return next(
                (e for e in ends if e[0] != node and is_ip_node(net_env, e[0])), None
            )
    return None


def _ip_mtu(output: str) -> int | None:
    match = re.search(r"\((\d+) is available to IP\)", output) or re.search(
        r"(?:actual-mtu|mtu)[ =:]+(\d+)", output, re.I
    )
    if match:
        return int(match[1])
    lines = output.splitlines()
    header = next(
        (line.split() for line in lines if "MTU" in line and "Interface" in line), None
    )
    if header:
        index = header.index("MTU")
        for line in lines:
            fields = line.split()
            if (
                fields
                and fields[0].startswith("Ethernet")
                and len(fields) > index
                and fields[index].isdigit()
            ):
                return int(fields[index])
    return None


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
    CONFUSABLE_WITH = ("icmp_frag_needed_filter_misconfiguration",)
    root_cause_owner = "interface"
    description = "Interface MTU is misconfigured and mismatches its link peer."
    TAGS: list[str] = ["link", "icmp"]
    native_router_operations = frozenset(
        {"interface_mtu", "set_interface_mtu", "save_config", "restore_config"}
    )
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
        original = getattr(self, "_original_mtus", {})
        if (host, intf) not in original:
            value = self._read_mtu(host, intf)
            if value is None:
                raise RuntimeError(f"Cannot read MTU on {host}:{intf}")
            original[host, intf] = value
            self._original_mtus = original
        ops = native_fault_ops(self, host, "set_interface_mtu")
        if ops is not None:
            if ops.profile == "iosxr":
                saved = getattr(self, "_native_mtu_configs", {})
                if host not in saved:
                    saved[host] = (ops, ops.save_config())
                    self._native_mtu_configs = saved
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

    def recover_fault(self, params: MtuMismatchParams) -> dict:
        original = getattr(self, "_original_mtus", {})
        saved = getattr(self, "_native_mtu_configs", {})
        for ops, config in saved.values():
            ops.restore_config(config)
        for (host, intf), mtu in original.items():
            if host not in saved:
                self._set_mtu(host, intf, mtu)
        observed = {
            f"{host}:{intf}": self._read_mtu(host, intf) for host, intf in original
        }
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(original)
            and all(observed[f"{h}:{i}"] == mtu for (h, i), mtu in original.items()),
            details={"observed_mtus": observed},
        )

    def oracle_playbook(self, params: MtuMismatchParams) -> OraclePlaybook:
        host, intf = params.host_name, params.intf_name
        steps = [
            interface_read(
                self.net_env,
                host,
                intf,
                f"Read the MTU of {host}:{intf}.",
                Check(
                    "the interface has a reduced MTU",
                    lambda out: _ip_mtu(out) == params.mtu,
                ),
                evidence=True,
                save_as="mtu_local",
            )
        ]
        peer = _segment_neighbor(self.net_env, host, intf)
        if peer is not None:
            far, far_intf = peer
            steps.append(
                interface_read(
                    self.net_env,
                    far,
                    far_intf,
                    f"Compare the MTU of {far}:{far_intf} with {host}:{intf}.",
                    compare(
                        "the link ends have different MTUs",
                        lambda out, outputs: (
                            _ip_mtu(out) is not None
                            and _ip_mtu(outputs.get("mtu_local", "")) is not None
                            and _ip_mtu(out) != _ip_mtu(outputs["mtu_local"])
                        ),
                    ),
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=f"{host}:{intf} has a reduced MTU that differs from its peer.",
            steps=steps,
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
