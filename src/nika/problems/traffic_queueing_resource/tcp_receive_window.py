"""Receiver TCP receive-window limited failure (Dapper receiver-limited)."""

from __future__ import annotations

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import first
from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import (
    DEFAULT_BDP_DIVISOR,
    DEFAULT_BUFFER_CEIL_BYTES,
    DEFAULT_BUFFER_FLOOR_BYTES,
    SYSCTL_MODERATE,
    SYSCTL_RMEM_MAX,
    SYSCTL_TCP_RMEM,
    SysctlSnapshot,
    format_tcp_rmem,
    interface_is_up,
    measure_path_baseline,
    read_sysctl_snapshot,
    sysctl_get,
    write_sysctl_snapshot,
)
from nika.utils.logger import system_logger


class TcpReceiveWindowLimitedParams(BaseModel):
    """Parameters for constraining the TCP receiver receive buffer."""

    host_name: str = Field(description="TCP receiver host (HTTP client).")
    sender_host: str = Field(
        default="hq_srv",
        description="HTTP sender host used for calibration and probes.",
    )
    sender_ip: str = Field(
        default="10.0.20.2",
        description="IPv4 address of the HTTP sender.",
    )
    small_url: str = Field(
        default="http://10.0.20.2/small.bin",
        description="Small-object URL (should remain roughly healthy).",
    )
    large_url: str = Field(
        default="http://10.0.20.2/large.bin",
        description="Large-object URL used for bulk-transfer measurement.",
    )
    bdp_divisor: float = Field(
        default=DEFAULT_BDP_DIVISOR,
        description="Initial calibration: target_buffer ≈ BDP / divisor.",
    )
    buffer_floor_bytes: int = Field(
        default=DEFAULT_BUFFER_FLOOR_BYTES,
        description="Lower clamp for injected receive-buffer size.",
    )
    buffer_ceil_bytes: int = Field(
        default=DEFAULT_BUFFER_CEIL_BYTES,
        description="Upper clamp for injected receive-buffer size.",
    )
    baseline_trials: int = Field(
        default=3,
        description="Number of calibration trials for healthy median throughput.",
    )
    iperf_duration_sec: int = Field(
        default=5,
        description="iperf3 duration (seconds) per calibration trial.",
    )


class TcpReceiveWindowLimited(ProblemBase):
    """Receiver TCP receive buffer undersized relative to path BDP."""

    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    root_cause_name: str = "tcp_receive_window_limited"
    description = "Receiver TCP window/buffer is too small for the path."
    TAGS: list[str] = ["http"]
    # Temporary: needs privileged hosts + enough path BDP (WAN RTT).
    COMPATIBLE_COLUMNS = frozenset({"enterprise_branch"})
    Params = TcpReceiveWindowLimitedParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        # Fault the HTTP client (TCP receiver of server->client bulk download).
        if ctx.scenario != "enterprise_branch":
            return {"host_name": ctx.host0}
        host_pool = ctx.host_pool
        return {
            "host_name": (
                "br1_corp_pc"
                if "br1_corp_pc" in host_pool
                else (first(host_pool) or ctx.host0)
            ),
            "sender_host": "hq_srv",
            "sender_ip": "10.0.20.2",
            "small_url": "http://10.0.20.2/small.bin",
            "large_url": "http://10.0.20.2/large.bin",
        }

    symptom_desc = (
        "Receiver TCP receive-buffer configuration is undersized relative to "
        "path BDP, so advertised RWND limits sustained bulk TCP throughput. "
        "Large HTTP downloads become substantially slower while ping, TCP "
        "connect, and small HTTP requests remain healthy."
    )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._original: SysctlSnapshot | None = None
        self._injected: SysctlSnapshot | None = None
        self._rmem_max_applied: bool = False
        self._baseline_bdp_bytes: float | None = None
        self._target_buffer_bytes: int | None = None
        self._healthy_throughput_bps: float | None = None
        self._healthy_rtt_ms: float | None = None

    def root_cause_resources(self, params: TcpReceiveWindowLimitedParams):
        return [node_resource(params.host_name)]

    def inject_fault(self, params: TcpReceiveWindowLimitedParams) -> None:
        baseline = measure_path_baseline(
            self.runtime,
            receiver=params.host_name,
            sender_host=params.sender_host,
            sender_ip=params.sender_ip,
            large_url=params.large_url,
            trials=params.baseline_trials,
            iperf_duration_sec=params.iperf_duration_sec,
            bdp_divisor=params.bdp_divisor,
            buffer_floor=params.buffer_floor_bytes,
            buffer_ceil=params.buffer_ceil_bytes,
        )
        self._healthy_throughput_bps = baseline.throughput_bps
        self._healthy_rtt_ms = baseline.rtt_ms
        self._baseline_bdp_bytes = baseline.bdp_bytes
        self._target_buffer_bytes = baseline.target_buffer_bytes

        # Restore state stays in memory; a file on the receiver would expose it.
        self._original = read_sysctl_snapshot(self.runtime, params.host_name)

        rmem = format_tcp_rmem(baseline.target_buffer_bytes)
        injected = SysctlSnapshot(
            moderate_rcvbuf="0",
            tcp_rmem=rmem,
            rmem_max=str(baseline.target_buffer_bytes),
        )
        self._rmem_max_applied = write_sysctl_snapshot(
            self.runtime, params.host_name, injected, require_rmem_max=False
        )
        self._injected = injected
        system_logger.info(
            "Injected tcp_receive_window_limited on %s: "
            "bdp=%.0fB target=%dB healthy_bps=%.0f rtt=%.1fms rmem_max_applied=%s",
            params.host_name,
            baseline.bdp_bytes,
            baseline.target_buffer_bytes,
            baseline.throughput_bps,
            baseline.rtt_ms,
            self._rmem_max_applied,
        )

    def verify_fault(self, params: TcpReceiveWindowLimitedParams) -> dict:
        moderate = sysctl_get(self.runtime, params.host_name, SYSCTL_MODERATE)
        tcp_rmem = sysctl_get(self.runtime, params.host_name, SYSCTL_TCP_RMEM)
        rmem_max = sysctl_get(self.runtime, params.host_name, SYSCTL_RMEM_MAX)
        iface_up = interface_is_up(self.runtime, params.host_name, "eth0")

        expected = self._injected
        if expected is None and self._target_buffer_bytes is not None:
            expected = SysctlSnapshot(
                moderate_rcvbuf="0",
                tcp_rmem=format_tcp_rmem(self._target_buffer_bytes),
                rmem_max=str(self._target_buffer_bytes),
            )

        moderate_ok = moderate == "0"
        rmem_ok = False
        rmem_max_ok = True
        if expected is not None:
            # Kernel may normalize whitespace in tcp_rmem.
            got = " ".join(tcp_rmem.split())
            want = " ".join(expected.tcp_rmem.split())
            rmem_ok = got == want
            # net.core.rmem_max is often read-only in containers; require match
            # only when the inject write succeeded.
            if self._rmem_max_applied:
                rmem_max_ok = rmem_max.strip() == expected.rmem_max.strip()
        verified = bool(moderate_ok and rmem_ok and rmem_max_ok and iface_up)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "receiver": params.host_name,
                "tcp_moderate_rcvbuf": moderate,
                "tcp_rmem": tcp_rmem,
                "rmem_max": rmem_max,
                "rmem_max_applied": self._rmem_max_applied,
                "iface_up": iface_up,
                "original": (
                    None
                    if self._original is None
                    else {
                        "tcp_moderate_rcvbuf": self._original.moderate_rcvbuf,
                        "tcp_rmem": self._original.tcp_rmem,
                        "rmem_max": self._original.rmem_max,
                    }
                ),
                "injected": (
                    None
                    if expected is None
                    else {
                        "tcp_moderate_rcvbuf": expected.moderate_rcvbuf,
                        "tcp_rmem": expected.tcp_rmem,
                        "rmem_max": expected.rmem_max,
                    }
                ),
                "estimated_bdp_bytes": self._baseline_bdp_bytes,
                "target_buffer_bytes": self._target_buffer_bytes,
                "healthy_throughput_bps": self._healthy_throughput_bps,
                "healthy_rtt_ms": self._healthy_rtt_ms,
            },
        )

    def recover_fault(self, params: TcpReceiveWindowLimitedParams) -> dict:
        original = self._original
        if original is None:
            return {
                "verified": False,
                "details": {"error": "original_sysctl_missing"},
            }
        write_sysctl_snapshot(
            self.runtime,
            params.host_name,
            original,
            require_rmem_max=False,
        )
        restored = read_sysctl_snapshot(self.runtime, params.host_name)
        ok = (
            restored.moderate_rcvbuf == original.moderate_rcvbuf
            and " ".join(restored.tcp_rmem.split())
            == " ".join(original.tcp_rmem.split())
            and restored.rmem_max.strip() == original.rmem_max.strip()
        )
        system_logger.info(
            "recover_fault tcp_receive_window_limited on %s: ok=%s",
            params.host_name,
            ok,
        )
        return {
            "verified": ok,
            "details": {
                "receiver": params.host_name,
                "restored": {
                    "tcp_moderate_rcvbuf": restored.moderate_rcvbuf,
                    "tcp_rmem": restored.tcp_rmem,
                    "rmem_max": restored.rmem_max,
                },
            },
        }
