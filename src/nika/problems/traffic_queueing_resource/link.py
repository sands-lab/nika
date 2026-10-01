import re
import time

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
    resolve_default_intf,
)
from nika.problems.rca.models import UnresolvedRootCauseError
from nika.utils.logger import system_logger
from nika.traffic.burst import BurstTrafficGenerator

# ==================================================================
# Problem: incast through a shallow egress buffer.
# ==================================================================
#
# Several senders burst to one receiver at the same time. The bursts converge on
# the last-hop egress port toward the receiver, whose buffer is too shallow to
# absorb them, so the port tail-drops although average load stays below the
# port rate. The root cause is the shallow egress queue on that port.

_TC_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmg]?)b?$", re.IGNORECASE)
_TBF_LIMIT_RE = re.compile(r"\btbf\b.*?\blimit\s+(\S+)", re.IGNORECASE)
# Columns where the receiver hangs off a point-to-point forwarding hop.
_INCAST_COLUMNS = frozenset(
    {
        "campus_lan",
        "dc_clos",
        "enterprise_branch",
        "p4_dc_fabric",
        "p4_dc_gateway",
        "sdn_l3_clos",
    }
)
# (port_rate, sender_rate) defaults per forwarding plane. BMv2 forwards in
# software at roughly 10 Mbit/s, so a 100mbit egress never queues behind it; the
# emulated port must drain slower than the switch forwards for bursts to pile up.
_KERNEL_RATES = ("100mbit", "20M/64")
# Python UDP senders sustain about 3–4 Mbit/s through BMv2 on the audit host;
# a 1 Mbit/s receiver policer therefore keeps dropping throughout the window.
# simple_switch transmits with PACKET_QDISC_BYPASS, so the
# switch root qdisc never counts those packets; the receiver ingress police
# is the queue the symptom reads.
_BMV2_RATES = ("1mbit", "3M/64")


def tc_size_bytes(value: str) -> int | None:
    """Parse a tc byte size such as ``16kb``, ``16Kb`` or ``16384b``."""
    match = _TC_SIZE_RE.match(value.strip())
    if match is None:
        return None
    scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2).lower()]
    return int(float(match.group(1)) * scale)


class IncastTrafficNetworkLimitationParams(BaseModel):
    """Parameters for injecting incast through a shallow egress buffer."""

    host_name: str = Field(description="Incast receiver (web server) host name.")
    forwarding_device: str | None = Field(
        default=None,
        description=(
            "Switch or router that forwards to the receiver; defaults to the "
            "topology peer of the receiver's eth0."
        ),
    )
    egress_intf: str | None = Field(
        default=None,
        description="Egress interface on forwarding_device toward the receiver.",
    )
    queue_limit: str = Field(
        default="16kb",
        description="Shallow egress buffer size (tc byte size).",
    )
    port_rate: str | None = Field(
        default=None,
        description=(
            "Egress port line rate the queue drains at; defaults to 100mbit, or "
            "1mbit when the forwarding device is a BMv2 switch."
        ),
    )
    port_burst: str = Field(
        default="32kb",
        description="Token-bucket depth for the emulated port rate.",
    )
    sender_count: int = Field(
        default=4, ge=2, description="Maximum number of synchronized senders."
    )
    sender_rate: str | None = Field(
        default=None,
        description=(
            "Per-sender iperf3 bitrate with burst packet count. Defaults to "
            "20M/64, or 3M/64 when the forwarding device is a BMv2 switch."
        ),
    )
    packet_size: int = Field(default=1400, description="UDP payload bytes.")
    duration: int = Field(default=300, description="Burst traffic seconds.")
    seed: int = Field(default=7, description="Deterministic flow-port seed.")
    probe_dst_ip: str | None = Field(
        default=None,
        description="ICMP-reachable IP of the receiver for symptom probes.",
    )
    observer_device: str | None = Field(
        default=None,
        description="Optional probe source host for path symptom checks.",
    )


class IncastTrafficNetworkLimitation(ProblemBase):
    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    root_cause_name: str = "incast_traffic_network_limitation"
    description = (
        "An egress port buffer is too shallow to absorb synchronized many-to-one "
        "bursts toward one receiver."
    )
    TAGS: str = ["http"]
    COMPATIBLE_COLUMNS = _INCAST_COLUMNS

    Params = IncastTrafficNetworkLimitationParams

    def __init__(self, scenario_name: str = "dc_clos", **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.scenario_name = scenario_name
        self._senders: list[str] = []
        self._receiver_ip: str | None = None
        self._burst_pids: dict[str, int] = {}

    def root_cause_resources(self, params: IncastTrafficNetworkLimitationParams):
        device, intf = self._egress_port(params)
        return [interface_on(self.net_env, device, intf)]

    def _egress_port(
        self, params: IncastTrafficNetworkLimitationParams
    ) -> tuple[str, str]:
        if params.forwarding_device and params.egress_intf:
            return params.forwarding_device, params.egress_intf
        needle = f"{params.host_name}:{resolve_default_intf('eth0', self.net_env)}"
        for _key, tps in iter_link_termination_points(self.net_env):
            endpoints = [str(ep) for ep in tps]
            if needle not in endpoints or len(endpoints) != 2:
                continue
            other = endpoints[0] if endpoints[1] == needle else endpoints[1]
            device, intf = parse_endpoint(other)
            if device and intf and device == (params.forwarding_device or device):
                return device, intf
        raise UnresolvedRootCauseError(
            f"{type(self).__name__}: {params.host_name} has no point-to-point "
            "forwarding hop; pass forwarding_device and egress_intf."
        )

    def _sender_pool(self, receiver: str) -> list[str]:
        servers = getattr(self.net_env, "servers", None) or {}
        pool = list(self.net_env.hosts or [])
        for role in ("web", "dns"):
            pool.extend(servers.get(role) or [])
        return sorted({h for h in pool if h != receiver})

    def _host_ip(self, host: str) -> str:
        # Same lookup BurstTrafficGenerator uses for the destination address.
        return self.runtime.exec(
            host, "hostname -I | awk '{print $1}'", timeout=10
        ).strip()

    def _pick_senders(self, params: IncastTrafficNetworkLimitationParams) -> list[str]:
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

    def inject_fault(self, params: IncastTrafficNetworkLimitationParams):
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
        self._incast_observe = (device, intf)
        if device in (self.net_env.bmv2_switches or []):
            self._install_bmv2_ingress_police(
                params.host_name, port_rate, params.port_burst
            )
            self._incast_observe = (params.host_name, "eth0")
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
        system_logger.info(
            f"Injected incast: egress {device}:{intf} queue limit "
            f"{params.queue_limit} at {port_rate}; senders {self._senders} "
            f"burst {sender_rate} to {params.host_name}."
        )

    def _install_bmv2_ingress_police(
        self, receiver: str, rate: str, burst: str
    ) -> None:
        """Police packets arriving at the receiver.

        simple_switch transmits with PACKET_QDISC_BYPASS, so a root qdisc on
        the switch port does not see the incast. The receiver ingress filter
        is the queue those packets actually enter.
        """
        self.runtime.exec(
            receiver,
            "tc qdisc del dev eth0 ingress >/dev/null 2>&1 || true; "
            "tc qdisc add dev eth0 handle ffff: ingress && "
            "tc filter add dev eth0 parent ffff: protocol ip prio 1 "
            "u32 match u32 0 0 "
            f"police rate {rate} burst {burst} drop",
            timeout=20,
        )

    def _queue_limit_bytes(self, device: str, intf: str) -> tuple[int | None, str]:
        output = self.runtime.tc_show_intf(device, intf).strip()
        match = _TBF_LIMIT_RE.search(output)
        return (tc_size_bytes(match.group(1)) if match else None), output

    def _fan_in_senders(
        self, params: IncastTrafficNetworkLimitationParams
    ) -> list[str]:
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

    def verify_fault(self, params: IncastTrafficNetworkLimitationParams) -> dict:
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

    def recover_fault(self, params: IncastTrafficNetworkLimitationParams) -> dict:
        """Remove the shallow egress queue and stop the burst senders."""
        device, intf = self._egress_port(params)
        for host, pid in self._burst_pids.items():
            try:
                self.runtime.exec(host, f"kill {pid} 2>/dev/null || true", timeout=10)
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
        if device in (self.net_env.bmv2_switches or []):
            try:
                self.runtime.exec(
                    params.host_name,
                    "tc qdisc del dev eth0 ingress >/dev/null 2>&1 || true",
                    timeout=15,
                )
            except Exception:  # noqa: BLE001
                pass
        observed, tc_output = self._queue_limit_bytes(device, intf)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=observed is None,
            details={"egress": f"{device}:{intf}", "tc_output": tc_output},
        )
