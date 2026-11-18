"""0.1.0 ``incast_traffic_network_limitation``: the failure starts its senders.

The current failure installs only the shallow queue and relies on the scenario's
fan-in workload. Release 0.1.0 cases run on scenarios without such a workload,
so this version also starts the synchronized burst senders toward the receiver.
"""

from __future__ import annotations

import re
import time

from pydantic import Field

from nika.problems.base import build_verify_result
from nika.problems.support.compatible_columns import NON_K8S_HOST_COLUMNS
from nika.problems.traffic_queueing_resource.link import (
    IncastTrafficNetworkLimitation,
    IncastTrafficNetworkLimitationParams,
    tc_size_bytes,
)
from nika.traffic.burst import BurstTrafficGenerator
from nika.utils.logger import system_logger

_TBF_LIMIT_RE = re.compile(r"\btbf\b.*?\blimit\s+(\S+)", re.IGNORECASE)
# (port_rate, sender_rate) per forwarding plane. BMv2 forwards in software at
# roughly 10 Mbit/s, so the emulated port must drain slower than the switch
# forwards for bursts to pile up.
_KERNEL_RATES = ("100mbit", "20M/64")
_BMV2_RATES = ("1mbit", "500K")


class IncastParams010(IncastTrafficNetworkLimitationParams):
    queue_limit: str | None = Field(default="16kb")
    port_burst: str = Field(default="32kb")
    sender_count: int = Field(default=4, ge=2)
    sender_rate: str | None = None
    packet_size: int = 1400
    duration: int = Field(default=3600, gt=0)
    seed: int = 7


class IncastTrafficNetworkLimitation010(IncastTrafficNetworkLimitation):
    dynamic_artifact = True
    COMPATIBLE_COLUMNS = NON_K8S_HOST_COLUMNS
    SYMPTOM_WORKLOAD = None
    Params = IncastParams010

    @classmethod
    def symptom_workload_target(cls, inject):
        return None

    def __init__(self, scenario_name: str = "dc_clos", **kwargs):
        super().__init__(scenario_name, **kwargs)
        self._senders: list[str] = []
        self._receiver_ip: str | None = None
        self._burst_pids: dict[str, int] = {}
        self._sink_pids: list[int] = []

    def _sender_pool(self, receiver: str) -> list[str]:
        servers = getattr(self.net_env, "servers", None) or {}
        pool = list(self.net_env.hosts or [])
        for role in ("web", "dns"):
            pool.extend(servers.get(role) or [])
        return sorted({h for h in pool if h != receiver})

    def _host_ip(self, host: str) -> str:
        return self.runtime.exec(
            host, "hostname -I | awk '{print $1}'", timeout=10
        ).strip()

    def _pick_senders(self, params: IncastParams010) -> list[str]:
        receiver_ip = self._host_ip(params.host_name)
        if not receiver_ip:
            raise RuntimeError(f"Cannot resolve an IPv4 address for {params.host_name}")
        self._receiver_ip = receiver_ip
        senders: list[str] = []
        for host in self._sender_pool(params.host_name):
            has_iperf = self.runtime.exec(
                host, "command -v iperf3 >/dev/null && echo yes || echo no", timeout=10
            ).strip()
            if has_iperf == "yes" and self.runtime.ping_ok(host, receiver_ip, count=1):
                senders.append(host)
            if len(senders) == params.sender_count:
                break
        if len(senders) < 2:
            raise RuntimeError(
                f"incast needs at least 2 senders that reach {params.host_name}; "
                f"found {senders}"
            )
        return senders

    def inject_fault(self, params: IncastParams010):
        device, intf = self._egress_port(params)
        default_port, default_sender = (
            _BMV2_RATES
            if device in (self.net_env.bmv2_switches or [])
            else _KERNEL_RATES
        )
        port_rate = params.port_rate or default_port
        sender_rate = params.sender_rate or default_sender
        self._senders = self._pick_senders(params)
        self.runtime.tc_set_tbf(
            host_name=device,
            intf_name=intf,
            rate=port_rate,
            burst=params.port_burst,
            limit=params.queue_limit,
        )
        burst = BurstTrafficGenerator(self.runtime).run(
            sources=self._senders,
            destination=params.host_name,
            protocol="udp",
            rate=sender_rate,
            packet_size=params.packet_size,
            duration=params.duration,
            synchronized_start=time.time() + 2.0,
            seed=params.seed,
            raw_udp=device in (self.net_env.bmv2_switches or []),
        )
        self._burst_pids = burst["sender_pids"]
        self._sink_pids = burst["sink_pids"]
        system_logger.info(
            f"Injected incast: egress {device}:{intf} queue limit "
            f"{params.queue_limit} at {port_rate}; senders {self._senders} "
            f"burst {sender_rate} to {params.host_name}."
        )

    def _queue_limit_bytes(self, device: str, intf: str) -> tuple[int | None, str]:
        output = self.runtime.tc_show_intf(device, intf).strip()
        match = _TBF_LIMIT_RE.search(output)
        return (tc_size_bytes(match.group(1)) if match else None), output

    def _fan_in_senders(self, params: IncastParams010) -> list[str]:
        if self._burst_pids:
            return [
                host
                for host, pid in self._burst_pids.items()
                if self.runtime.exec(
                    host, f"kill -0 {pid} 2>/dev/null && echo yes || true", timeout=10
                ).strip()
                == "yes"
            ]
        receiver_ip = self._receiver_ip or self._host_ip(params.host_name)
        senders = self._senders or self._sender_pool(params.host_name)
        # ``-[c]`` keeps pgrep from matching the wrapper shell's own command line.
        pattern = f"iperf3 -[c] {receiver_ip} "
        return [
            host
            for host in senders
            if self.runtime.exec(
                host, f"pgrep -f '{pattern}' || true", timeout=10
            ).strip()
        ]

    def verify_fault(self, params: IncastParams010) -> dict:
        """Verify the shallow egress queue and the running many-to-one bursts."""
        device, intf = self._egress_port(params)
        observed, tc_output = self._queue_limit_bytes(device, intf)
        expected = tc_size_bytes(params.queue_limit)
        queue_ok = (
            observed is not None
            and expected is not None
            and abs(observed - expected) <= 1024
        )
        active = self._fan_in_senders(params)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=queue_ok and len(active) >= 2,
            details={
                "egress": f"{device}:{intf}",
                "queue_limit_bytes": observed,
                "expected_queue_limit_bytes": expected,
                "tc_output": tc_output,
                "active_senders": active,
                "receiver": params.host_name,
            },
        )

    def recover_fault(self, params: IncastParams010) -> dict:
        """Remove the shallow egress queue and stop the burst senders."""
        device, intf = self._egress_port(params)
        for host, pid in self._burst_pids.items():
            try:
                self.runtime.exec(host, f"kill {pid} 2>/dev/null || true", timeout=10)
            except Exception:  # noqa: BLE001
                pass
        for pid in self._sink_pids:
            try:
                self.runtime.exec(
                    params.host_name, f"kill {pid} 2>/dev/null || true", timeout=10
                )
            except Exception:  # noqa: BLE001
                pass
        senders = self._senders or self._sender_pool(params.host_name)
        for host in [*senders, params.host_name]:
            try:
                self.runtime.exec(host, "pkill -f 'iperf3' >/dev/null 2>&1 || true")
            except Exception:  # noqa: BLE001
                pass
        try:
            self.runtime.tc_clear_intf(device, intf)
        except Exception:  # noqa: BLE001
            pass
        observed, tc_output = self._queue_limit_bytes(device, intf)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=observed is None,
            details={"egress": f"{device}:{intf}", "tc_output": tc_output},
        )
