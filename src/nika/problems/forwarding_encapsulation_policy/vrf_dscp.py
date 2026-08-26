"""CORP VRF DSCP mis-remarking on enterprise_branch Site Edge overlay egress."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

from traffic import corp_qos_compete as qos_traffic
from nika.net_env.enterprise_branch.topology import (
    DSCP_CS0,
    DSCP_EF,
    TOS_EF,
    TopoSize,
    dscp_remark_inject_targets,
)
from nika.net_env.verify import http_ok, ping_ok
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca import interface_resource
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

_NFT_TABLE = "nika_dscp"
_NFT_CHAIN = "POSTROUTING"
_SETTLE_SEC = 4
_PROBE_PORT = 5198


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
    overlay iface so realtime flows share the congested BE class.
    """

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name: str = "vrf_dscp_remarking"
    TAGS: list[str] = ["vpn"]
    Params = VrfDscpRemarkingParams
    symptom_desc = (
        "High-priority CORP realtime traffic that should keep DSCP EF is "
        "incorrectly remarked to CS0/BE at a Site Edge LAN→overlay boundary. "
        "Under competing bulk load on the same WireGuard egress, the realtime "
        "flow shows elevated latency, jitter, or loss while WireGuard, eBGP, "
        "VRF RIB, and other VRFs remain healthy."
    )
    supported_backends: tuple[str, ...] = ("kathara",)

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._workload: qos_traffic.CompeteHandle | None = None
        self._baseline: qos_traffic.FlowMetrics | None = None
        self._corp_prefix: str | None = None

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
            if (
                target.edge == params.host_name
                and target.intf_name == params.intf_name
                and target.src_host == params.src_host
                and target.dst_host == params.dst_host
            ):
                return target.corp_prefix
        # Fallback: match edge+iface only.
        for target in dscp_remark_inject_targets(size):
            if target.edge == params.host_name and target.intf_name == params.intf_name:
                return target.corp_prefix
        site = (
            params.host_name[: -len("_edge")]
            if params.host_name.endswith("_edge")
            else params.host_name
        )
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
        size = self._topo_size()
        self._corp_prefix = self._resolve_corp_prefix(params)
        matrix = qos_traffic.build_compete_matrix(
            self.runtime,
            topo_size=size,
            edge=params.host_name,
            intf_name=params.intf_name,
            src_host=params.src_host,
            dst_host=params.dst_host,
        )
        self._workload = qos_traffic.start(self.runtime, matrix, duration_sec=600)
        time.sleep(_SETTLE_SEC)
        self._baseline = qos_traffic.measure(self._workload)
        self.logger.info(
            f"QoS baseline on {params.host_name}/{params.intf_name}: "
            f"latency={self._baseline.latency_ms} jitter={self._baseline.jitter_ms} "
            f"loss={self._baseline.lost_percent}"
        )
        self._install_remark(params, self._corp_prefix)
        time.sleep(_SETTLE_SEC)

    def _install_remark(self, params: VrfDscpRemarkingParams, corp_prefix: str) -> None:
        edge = params.host_name
        iface = params.intf_name
        # Ensure mangle table/chain, then EF→CS0 on CORP traffic leaving overlay.
        cmds = [
            f"nft add table ip {_NFT_TABLE} 2>/dev/null || true",
            f"nft 'add chain ip {_NFT_TABLE} {_NFT_CHAIN} "
            "{ type filter hook postrouting priority mangle ; }' 2>/dev/null || true",
            f"nft flush chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true",
            f"nft add rule ip {_NFT_TABLE} {_NFT_CHAIN} "
            f'oifname "{iface}" ip saddr {corp_prefix} ip dscp {DSCP_EF} '
            f"counter ip dscp set {DSCP_CS0}",
        ]
        for cmd in cmds:
            self.runtime.exec(edge, cmd)
        self.logger.info(
            f"Injected DSCP EF→CS0 remark on {edge} oif={iface} saddr={corp_prefix}"
        )

    def _remark_present(self, params: VrfDscpRemarkingParams) -> bool:
        out = self.runtime.exec(
            params.host_name,
            f"nft list chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true",
        )
        return (
            params.intf_name in out
            and ("dscp set 0" in out or "dscp set cs0" in out.lower())
            and ("dscp ef" in out.lower() or f"dscp {DSCP_EF}" in out)
        )

    def _remove_remark(self, params: VrfDscpRemarkingParams) -> None:
        self.runtime.exec(
            params.host_name,
            f"nft flush chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true; "
            f"nft delete chain ip {_NFT_TABLE} {_NFT_CHAIN} 2>/dev/null || true; "
            f"nft delete table ip {_NFT_TABLE} 2>/dev/null || true",
        )

    def _smoke_ok(self, params: VrfDscpRemarkingParams) -> dict[str, bool]:
        checks: dict[str, bool] = {}
        link = self.runtime.exec(
            params.host_name, f"ip -o link show {params.intf_name}"
        ).strip()
        checks["wg_iface_up"] = bool(link) and "state DOWN" not in link
        checks["ef_path_ping"] = ping_ok(
            self.runtime,
            params.src_host,
            qos_traffic.host_ip(self.runtime, params.dst_host),
        )
        checks["hq_server_http"] = http_ok(
            self.runtime, params.src_host, "http://10.0.20.2/"
        ) or http_ok(self.runtime, "br1_corp_pc", "http://10.0.20.2/")
        bgp = self.runtime.exec(params.host_name, "vtysh -c 'show bgp summary'")
        checks["bgp_alive"] = any(
            len(line.split()) >= 10 and line.split()[9].isdigit()
            for line in bgp.splitlines()
        )
        corp_rib = self.runtime.exec(
            params.host_name,
            "vtysh -c 'show ip route vrf vrf_corp' 2>/dev/null || true",
        )
        if not corp_rib.strip():
            corp_rib = self.runtime.exec(
                params.host_name, "ip route show vrf vrf_corp 2>/dev/null || true"
            )
        checks["vrf_corp_rib"] = "10." in corp_rib
        return checks

    def _metrics_dict(
        self, metrics: qos_traffic.FlowMetrics | None
    ) -> dict[str, Any] | None:
        if metrics is None:
            return None
        return {
            "latency_ms": metrics.latency_ms,
            "latency_mdev_ms": metrics.latency_mdev_ms,
            "jitter_ms": metrics.jitter_ms,
            "lost_percent": metrics.lost_percent,
            "bits_per_second": metrics.bits_per_second,
            "packets": metrics.packets,
            "error": metrics.error,
        }

    def verify_fault(self, params: VrfDscpRemarkingParams) -> dict[str, Any]:
        remark_ok = self._remark_present(params)
        dst_ip = qos_traffic.host_ip(self.runtime, params.dst_host)

        # Pause bulk so DSCP probes are not dropped by the short BE FIFO.
        if self._workload is not None:
            qos_traffic.pause_bulk(self._workload)
            time.sleep(1.0)

        src_tos = qos_traffic.sample_dscp_tos(
            self.runtime,
            capture_host=params.src_host,
            capture_iface="eth0",
            src_host=params.src_host,
            dst_ip=dst_ip,
            send_tos=TOS_EF,
        )
        edge_tos = qos_traffic.sample_dscp_tos(
            self.runtime,
            capture_host=params.host_name,
            capture_iface=params.intf_name,
            src_host=params.src_host,
            dst_ip=dst_ip,
            send_tos=TOS_EF,
        )
        dst_tos = qos_traffic.sample_dscp_tos(
            self.runtime,
            capture_host=params.dst_host,
            capture_iface="eth0",
            src_host=params.src_host,
            dst_ip=dst_ip,
            send_tos=TOS_EF,
        )
        samples_confirm = (
            src_tos == TOS_EF and dst_tos == 0 and (edge_tos is None or edge_tos == 0)
        )

        if self._workload is not None:
            qos_traffic.resume_bulk(self._workload)
            time.sleep(2.0)

        details: dict[str, Any] = {
            "remark_present": remark_ok,
            "src_tos": src_tos,
            "edge_tos": edge_tos,
            "dst_tos": dst_tos,
            "samples_confirm": samples_confirm,
        }
        if self._workload is not None and self._baseline is not None:
            current = qos_traffic.measure(self._workload)
            perf_ok = qos_traffic.degraded(self._baseline, current)
            smoke = self._smoke_ok(params)
            details.update(
                {
                    "baseline": self._metrics_dict(self._baseline),
                    "current": self._metrics_dict(current),
                    "perf_degraded": perf_ok,
                    "smoke": smoke,
                    "symptom": (
                        "CORP EF traffic is remarked to CS0 at the Site Edge overlay "
                        "egress and, under competing bulk load, shows elevated "
                        "latency/jitter/loss versus the healthy baseline."
                    ),
                }
            )
        # Inject gate: remark artifact. DSCP/perf evidence retained in details for matrix.
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(remark_ok),
            details=details,
        )

    def recover_fault(self, params: VrfDscpRemarkingParams) -> dict[str, Any]:
        """Remove remarking, confirm DSCP/performance recovery, then stop workload."""
        self._remove_remark(params)
        time.sleep(2.0)
        recovered_metrics = None
        if self._workload is not None:
            recovered_metrics = qos_traffic.measure(self._workload)

        dst_ip = qos_traffic.host_ip(self.runtime, params.dst_host)
        if self._workload is not None:
            qos_traffic.pause_bulk(self._workload)
            time.sleep(1.0)
        dst_tos = qos_traffic.sample_dscp_tos(
            self.runtime,
            capture_host=params.dst_host,
            capture_iface="eth0",
            src_host=params.src_host,
            dst_ip=dst_ip,
            send_tos=TOS_EF,
        )
        edge_tos = qos_traffic.sample_dscp_tos(
            self.runtime,
            capture_host=params.host_name,
            capture_iface=params.intf_name,
            src_host=params.src_host,
            dst_ip=dst_ip,
            send_tos=TOS_EF,
        )
        if self._workload is not None:
            qos_traffic.resume_bulk(self._workload)
        dscp_restored = dst_tos == TOS_EF and (edge_tos is None or edge_tos == TOS_EF)
        perf_restored = True
        if self._baseline is not None and recovered_metrics is not None:
            perf_restored = not qos_traffic.degraded(
                self._baseline,
                recovered_metrics,
                latency_factor=2.0,
                jitter_factor=2.0,
            )
        if self._workload is not None:
            qos_traffic.stop(self._workload)
            self._workload = None

        remark_gone = not self._remark_present(params)
        ok = remark_gone and dscp_restored and perf_restored
        details = {
            "remark_gone": remark_gone,
            "dst_tos": dst_tos,
            "edge_tos": edge_tos,
            "dscp_restored": dscp_restored,
            "perf_restored": perf_restored,
            "baseline": self._metrics_dict(self._baseline),
            "recovered": self._metrics_dict(recovered_metrics),
        }
        self.logger.info(f"recover_fault vrf_dscp_remarking: ok={ok} details={details}")
        return {"verified": ok, "details": details}
