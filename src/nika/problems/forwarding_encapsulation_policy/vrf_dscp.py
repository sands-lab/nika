"""CORP VRF DSCP mis-remarking on enterprise_branch Site Edge overlay egress."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

from nika.net_env.enterprise_branch.topology import (
    DSCP_CS0,
    DSCP_EF,
    TopoSize,
    dscp_remark_inject_targets,
)
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca import interface_resource
from nika.problems.support.benchmark_targets import replace, workload_exercises
from nika.problems.support.oracle import Check, OraclePlaybook, all_of, contains, shell
from nika.problems.support.router_ops import native_fault_ops
from nika.runtime.base import RuntimeCapabilityError
from nika.traffic.workload import REALTIME_BULK_CONTENTION
from nika.utils.logger import system_logger

# iptables-nft naming, as an operator QoS policy would use.
_NFT_TABLE = "mangle"
_NFT_CHAIN = "POSTROUTING"
# The scenario's EF media crosses the egress many times a second.
_MATCH_WAIT_SEC = 10.0
_COUNTER_RE = re.compile(r"counter packets (\d+)")


def _remark_targets(cls: type, net_env: Any, scenario: str) -> list[Any]:
    """Remark targets whose egress the scenario workload loads with EF + bulk."""
    targets = getattr(net_env, "dscp_remark_targets", None)
    if targets is None:
        raise ValueError(f"Scenario {scenario!r} has no Site Edge DSCP remark targets")
    return [
        target
        for target in targets()
        if workload_exercises(
            cls, net_env, {"host_name": target.edge, "intf_name": target.intf_name}
        )
    ]


def _counter_grows(output: str) -> bool:
    """Two counter readings taken a few seconds apart, the second higher."""
    readings = [int(n) for n in _COUNTER_RE.findall(output)]
    return len(readings) >= 2 and readings[-1] > readings[0] > 0


class VrfDscpRemarkingParams(BaseModel):
    """Parameters for EF→CS0 DSCP remarking on one Site Edge overlay egress."""

    host_name: str = Field(description="Site Edge router (e.g. br1_edge, hq_edge).")
    intf_name: str = Field(
        description="WireGuard overlay egress iface (e.g. wg_hq, wg_br1)."
    )
    src_host: str = Field(description="CORP host sourcing the EF foreground flow.")
    dst_host: str = Field(description="CORP host receiving the EF foreground flow.")
    direction: Literal["lan_to_overlay"] = Field(
        default="lan_to_overlay",
        description="Forwarding direction; only LAN→overlay is supported.",
    )
    corp_prefix: str | None = Field(
        default=None,
        description="Optional CORP LAN prefix for the fault site (auto-filled).",
    )


class VrfDscpRemarking(ProblemBase):
    """Wrong DSCP remarking of CORP EF traffic at a Site Edge overlay boundary.

    Healthy labs preserve DSCP and classify EF vs BE on WireGuard egress.
    Inject rewrites EF (46) to CS0 (0) for CORP traffic leaving the selected
    overlay iface. The scenario's own EF media then leaves its reserved class
    for best effort, where it competes with bulk transfers for less capacity
    than it needs and loses packets to the class's fair queueing.
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "degradation"
    root_cause_name: str = "vrf_dscp_remarking"
    root_cause_owner = "interface"
    description = "VRF edge incorrectly remarks high-priority DSCP."
    TAGS: list[str] = ["vpn"]
    native_router_operations = frozenset(
        {
            "add_dscp_remark",
            "bgp_established_peers",
            "dscp_remark_present",
            "interface_up",
            "remove_dscp_remark",
            "vrf_routes",
        }
    )
    Params = VrfDscpRemarkingParams
    BENCHMARK_TUNNEL_IFACE = True
    SYMPTOM_WORKLOAD = REALTIME_BULK_CONTENTION
    symptom_desc = (
        "High-priority CORP realtime traffic that should keep DSCP EF is "
        "incorrectly remarked to CS0/BE at a Site Edge LAN→overlay boundary. "
        "Under competing bulk load on the same WireGuard egress, the realtime "
        "flow loses packets and gains jitter at the receiving endpoint, and "
        "the egress EF class stops carrying it, while WireGuard, eBGP, VRF "
        "RIB, and other VRFs remain healthy."
    )
    supported_backends: tuple[str, ...] = ("kathara",)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._corp_prefix: str | None = None

    @classmethod
    def symptom_workload_target(cls, inject: Mapping[str, str]) -> tuple[str, ...]:
        return (inject.get("host_name") or "", inject.get("intf_name") or "")

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = _remark_targets(cls, ctx.net_env, ctx.scenario)
        if not targets:
            raise ValueError(
                "No DSCP remark inject targets for enterprise_branch "
                f"topo_size={ctx.topo_size!r}"
            )
        target = ctx.rng.choice(targets)
        return {
            "host_name": target.edge,
            "intf_name": target.intf_name,
            "src_host": target.src_host,
            "dst_host": target.dst_host,
            "direction": "lan_to_overlay",
            "corp_prefix": target.corp_prefix,
        }

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        return [
            replace(
                base,
                host_name=target.edge,
                intf_name=target.intf_name,
                src_host=target.src_host,
                dst_host=target.dst_host,
                corp_prefix=target.corp_prefix,
            )
            for target in _remark_targets(cls, ctx.net_env, ctx.scenario)
        ]

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.net_env.net_env_pool import is_enterprise_branch_scenario

        scenario, topo_size = ctx.scenario, ctx.topo_size
        if not is_enterprise_branch_scenario(scenario):
            raise ValueError(
                f"vrf_dscp_remarking requires enterprise_branch (got {scenario!r})"
            )
        eligible_keys = {
            (t.edge, t.intf_name, t.src_host, t.dst_host)
            for t in _remark_targets(cls, ctx.net_env, ctx.scenario)
        }
        key = (
            inject.get("host_name") or "",
            inject.get("intf_name") or "",
            inject.get("src_host") or "",
            inject.get("dst_host") or "",
        )
        if key not in eligible_keys:
            raise ValueError(
                f"vrf_dscp_remarking target {key!r} is not an eligible "
                f"LAN→overlay CORP path on {scenario} (topo_size={topo_size!r}); "
                f"eligible count={len(eligible_keys)}"
            )

    def root_cause_resources(self, params: VrfDscpRemarkingParams):
        return [interface_resource(params.host_name, params.intf_name)]

    def _topo_size(self) -> TopoSize:
        size = getattr(self.net_env, "topo_size", None) or "s"
        if size not in {"s", "m", "l"}:
            return "s"
        return size  # type: ignore[return-value]

    def _resolve_corp_prefix(self, params: VrfDscpRemarkingParams) -> str:
        if params.corp_prefix:
            return params.corp_prefix
        size = self._topo_size()
        for target in dscp_remark_inject_targets(size):
            if target.edge == params.host_name and target.intf_name == params.intf_name:
                return target.corp_prefix
        site = params.host_name.removesuffix("_edge")
        from nika.net_env.enterprise_branch.topology import (
            build_topo_spec,
        )

        spec = build_topo_spec(size)
        lan = next(lan for lan in spec.sites[site].lans if lan.role == "corp")
        return lan.prefix

    def inject_fault(self, params: VrfDscpRemarkingParams) -> None:
        match self.lab_backend:
            case "kathara":
                self._inject_kathara(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: "
                    f"unsupported backend {backend!r}."
                )

    def _inject_kathara(self, params: VrfDscpRemarkingParams) -> None:
        if params.direction != "lan_to_overlay":
            raise ValueError(
                f"Unsupported direction {params.direction!r}; "
                "only lan_to_overlay is supported."
            )
        self._corp_prefix = self._resolve_corp_prefix(params)
        edge = params.host_name
        iface = params.intf_name
        ops = native_fault_ops(self, edge, "add_dscp_remark")
        if ops is not None:
            ops.add_dscp_remark(iface, self._corp_prefix, DSCP_EF, DSCP_CS0)
            self.logger.info(
                f"Injected DSCP EF→CS0 remark on {edge} oif={iface} "
                f"saddr={self._corp_prefix} ({ops.profile})"
            )
            return
        # Ensure mangle table/chain, then EF→CS0 on CORP traffic leaving overlay.
        cmds = [
            f"nft add table ip {_NFT_TABLE} 2>/dev/null || true",
            f"nft 'add chain ip {_NFT_TABLE} {_NFT_CHAIN} "
            "{ type filter hook postrouting priority mangle ; }' 2>/dev/null || true",
            f"nft flush chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true",
            f"nft add rule ip {_NFT_TABLE} {_NFT_CHAIN} "
            f'oifname "{iface}" ip saddr {self._corp_prefix} '
            f"ip dscp {DSCP_EF} counter ip dscp set {DSCP_CS0}",
        ]
        for cmd in cmds:
            self.runtime.exec(edge, cmd)
        self.logger.info(
            f"Injected DSCP EF→CS0 remark on {edge} oif={iface} "
            f"saddr={self._corp_prefix}"
        )

    def oracle_playbook(self, params: VrfDscpRemarkingParams) -> OraclePlaybook:
        edge, iface = params.host_name, params.intf_name
        prefix = self._resolve_corp_prefix(params)
        rule = (
            rf'oifname "{iface}" ip saddr {re.escape(prefix)} ip dscp (ef|0x2e|46) '
            r".*ip dscp set (cs0|0x00|0)\b"
        )
        remark_counter = (
            f"nft -a list ruleset 2>/dev/null | grep -F 'oifname \"{iface}\"' "
            "| grep -E 'dscp set' | head -4"
        )
        return OraclePlaybook(
            finding=(
                f"A mangle rule on {edge} rewrites CORP DSCP EF to CS0 on overlay "
                f"egress {iface}, so realtime traffic falls out of the EF class "
                "into best effort and competes with bulk transfers."
            ),
            steps=[
                shell(
                    edge,
                    f"tc -s class show dev {iface} | head -30",
                    f"Inspect the EF and best-effort QoS classes on {edge}:{iface}.",
                ),
                shell(
                    edge,
                    "nft list ruleset 2>&1 | grep -nE 'table|chain|hook|dscp' | head -40",
                    f"Look for DSCP rewrite rules on {edge}.",
                    contains(rule, f"EF from {prefix} leaving {iface} is set to CS0"),
                    evidence=True,
                ),
                shell(
                    edge,
                    f"{remark_counter}; sleep 5; {remark_counter}",
                    "Check that live EF traffic keeps hitting the rewrite rule.",
                    all_of(
                        contains(rule, "the rewrite rule matches"),
                        Check("its packet counter grows", _counter_grows),
                    ),
                    evidence=True,
                    timeout=25,
                    attempts=2,
                ),
            ],
        )

    def _chain(self, params: VrfDscpRemarkingParams) -> str:
        return self.runtime.exec(
            params.host_name,
            f"nft list chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true",
        )

    def _remark_present(self, params: VrfDscpRemarkingParams) -> bool:
        ops = native_fault_ops(self, params.host_name, "dscp_remark_present")
        if ops is not None:
            return ops.dscp_remark_present(params.intf_name, DSCP_EF, DSCP_CS0)
        out = self._chain(params)
        return (
            params.intf_name in out
            and ("dscp set 0" in out or "dscp set cs0" in out.lower())
            and ("dscp ef" in out.lower() or f"dscp {DSCP_EF}" in out)
        )

    def _remarked_packets(self, params: VrfDscpRemarkingParams) -> int:
        match = _COUNTER_RE.search(self._chain(params))
        return int(match.group(1)) if match else 0

    def _remove_remark(self, params: VrfDscpRemarkingParams) -> None:
        ops = native_fault_ops(self, params.host_name, "remove_dscp_remark")
        if ops is not None:
            ops.remove_dscp_remark(params.intf_name, DSCP_EF, DSCP_CS0)
            return
        self.runtime.exec(
            params.host_name,
            f"nft flush chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true; "
            f"nft delete chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true; "
            f"nft delete table ip {_NFT_TABLE} 2>/dev/null || true",
        )

    def verify_fault(self, params: VrfDscpRemarkingParams) -> dict[str, Any]:
        """The remark rule is installed and live EF traffic is hitting it."""
        remark_ok = self._remark_present(params)
        ops = native_fault_ops(self, params.host_name, "dscp_remark_present")
        if ops is not None:
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=bool(remark_ok),
                details={
                    "remark_present": remark_ok,
                    "intf": params.intf_name,
                    "profile": ops.profile,
                },
            )
        remarked = self._remarked_packets(params)
        deadline = time.monotonic() + _MATCH_WAIT_SEC
        while remark_ok and remarked == 0 and time.monotonic() < deadline:
            time.sleep(1.0)
            remarked = self._remarked_packets(params)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(remark_ok and remarked > 0),
            details={
                "remark_present": remark_ok,
                "remarked_packets": remarked,
                "intf": params.intf_name,
            },
        )

    def recheck_artifact(self, params: VrfDscpRemarkingParams) -> dict[str, Any]:
        """Re-read the nft remark rule. Does not send traffic."""
        remark_ok = bool(self._remark_present(params))
        return {
            "present": remark_ok,
            "fault": self.root_cause_name,
            "scope": "artifact",
            "evidence": {"remark_present": remark_ok, "intf": params.intf_name},
            "error": None if remark_ok else "fault artifact absent",
        }

    def recover_fault(self, params: VrfDscpRemarkingParams) -> dict[str, Any]:
        """Remove the remark rule."""
        self._remove_remark(params)
        remark_gone = not self._remark_present(params)
        self.logger.info(f"recover_fault vrf_dscp_remarking: ok={remark_gone}")
        return {"verified": remark_gone, "details": {"remark_gone": remark_gone}}
