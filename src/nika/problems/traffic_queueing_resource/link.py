import json
import re
from dataclasses import dataclass

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
from nika.traffic.workload import FAN_IN_BURSTS
from nika.utils.logger import system_logger

# ==================================================================
# Problem: incast through a shallow egress buffer.
# ==================================================================
#
# Partition/aggregate incast: the scenario's business workload has several
# workers answer one receiver's query at the same moment, each with a block of
# datagrams sent back to back. The blocks converge on the last-hop egress port
# toward the receiver. A normal port buffer absorbs them; this failure leaves
# that port with a queue so shallow that the converging blocks overflow it, so
# the port tail-drops at every query although its average load stays far
# below the line rate. The root cause is the shallow egress queue meeting the
# synchronized fan-in; the port rate itself is an ordinary line rate, unlike
# link_capacity_bottleneck (sustained demand above a low rate).


@dataclass(frozen=True)
class PortProfile:
    """Line rate and queue depth of the emulated egress port."""

    port_rate: str
    queue_limit: str


# Emulated hops forward far below their nominal line rate: on the audit host a
# kernel-forwarded path carries about 36 Mbit/s of TCP, so a 40 Mbit/s port is
# not the bottleneck for one flow; its 16 KB queue lasts 3.3 ms.
_KERNEL_PROFILE = PortProfile("40mbit", "16kb")
# BMv2 forwards in software: one gateway-spine-leaf path sustains about
# 9.5 Mbit/s of 1400-byte UDP and a leaf about 15 Mbit/s, but a leaf forwards a
# short burst much faster than that. A 20 Mbit/s port is therefore never the
# bottleneck for a single flow; its 16 KB queue lasts 5.7 ms, less than four
# workers' converging 24-datagram answers need.
_BMV2_PROFILE = PortProfile("20mbit", "16kb")

_TC_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmg]?)b?$", re.IGNORECASE)
_TC_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)([kmg]?)bit$", re.IGNORECASE)


def tc_size_bytes(value: str) -> int | None:
    """Parse a tc byte size such as ``16kb``, ``16Kb`` or ``16384b``."""
    match = _TC_SIZE_RE.match(value.strip())
    if match is None:
        return None
    scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}[match.group(2).lower()]
    return int(float(match.group(1)) * scale)


def tc_rate_bps(value: str) -> float | None:
    """Parse a tc rate such as ``20mbit`` or ``100Mbit`` into bit/s."""
    match = _TC_RATE_RE.match(value.strip())
    if match is None:
        return None
    scale = {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[match.group(2).lower()]
    return float(match.group(1)) * scale


def tbf_shape(tc_json: str) -> dict | None:
    """Rate (bit/s), bucket and queue limit (bytes) of the root TBF in ``tc -j``.

    ``tc`` reports a TBF queue as a latency; the byte limit it was configured
    with is ``rate * latency + burst``.
    """
    try:
        qdiscs = json.loads(tc_json)
    except (TypeError, ValueError):
        return None
    for qdisc in qdiscs if isinstance(qdiscs, list) else []:
        if qdisc.get("kind") != "tbf" or not qdisc.get("root"):
            continue
        options = qdisc.get("options") or {}
        rate, burst = options.get("rate"), options.get("burst")
        if not isinstance(rate, int) or not isinstance(burst, int):
            return None
        limit = options.get("limit")
        lat = options.get("lat")
        if not isinstance(limit, int) and isinstance(lat, int | float):
            limit = round(rate * lat / 1e6 + burst)
        return {"rate_bps": rate * 8, "burst_bytes": burst, "limit_bytes": limit}
    return None


class IncastTrafficNetworkLimitationParams(BaseModel):
    """Parameters for injecting incast through a shallow egress buffer."""

    host_name: str = Field(
        description="Incast receiver: the node the scenario's workers answer at once."
    )
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
    queue_limit: str | None = Field(
        default=None,
        description=("Shallow egress buffer size (tc byte size); defaults to 16kb."),
    )
    port_rate: str | None = Field(
        default=None,
        description=(
            "Egress port line rate the queue drains at; defaults to 40mbit, or "
            "20mbit when the forwarding device is a BMv2 switch."
        ),
    )
    port_burst: str = Field(
        default="2kb",
        description="Token-bucket depth of the emulated port (about one frame).",
    )
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
    symptom_probe = "custom"
    symptom_class = "degradation"
    root_cause_name: str = "incast_traffic_network_limitation"
    description = (
        "An egress port buffer is too shallow to absorb synchronized many-to-one "
        "bursts toward one receiver."
    )
    TAGS: str = ["http"]
    # Scenarios whose business workload carries a partition/aggregate fan-in.
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway", "p4_dc_fabric"})
    SYMPTOM_WORKLOAD = FAN_IN_BURSTS

    Params = IncastTrafficNetworkLimitationParams
    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("host_name") or "",)

    @classmethod
    def benchmark_inject_params(cls, ctx):
        # The receiver is the node the scenario workload fans in to; the
        # observer probes it across the fabric from another leaf.
        scenario, net_env = ctx.scenario, ctx.net_env
        params: dict[str, str] = {}
        workload = net_env.business_workload()
        receivers = workload.targets(FAN_IN_BURSTS) if workload else []
        receiver = receivers[0][0] if receivers else ctx.web0 or ctx.host0
        if scenario == "p4_dc_fabric":
            model = net_env.model
            victim = next(w for w in model.web_endpoints() if w.name == receiver)
            observer = next(
                c for c in model.client_endpoints() if c.leaf_id != victim.leaf_id
            )
            params.update(
                host_name=victim.name,
                probe_dst_ip=victim.ip,
                observer_device=observer.name,
            )
        elif scenario == "p4_dc_gateway":
            model = net_env.model
            victim = next(s for s in model.services if s.name == receiver)
            params.update(
                host_name=victim.name,
                probe_dst_ip=victim.ip,
                observer_device=model.clients[0].name,
            )
        else:
            params["host_name"] = receiver
        return params

    def __init__(self, scenario_name: str = "dc_clos", **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.scenario_name = scenario_name

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

    def port_profile(self, params: IncastTrafficNetworkLimitationParams) -> PortProfile:
        """Egress port shape, with plane defaults filled in."""
        device, _intf = self._egress_port(params)
        bmv2 = device in (self.net_env.bmv2_switches or [])
        base = _BMV2_PROFILE if bmv2 else _KERNEL_PROFILE
        return PortProfile(
            port_rate=params.port_rate or base.port_rate,
            queue_limit=params.queue_limit or base.queue_limit,
        )

    def inject_fault(self, params: IncastTrafficNetworkLimitationParams):
        device, intf = self._egress_port(params)
        profile = self.port_profile(params)
        self.runtime.tc_set_tbf(
            host_name=device,
            intf_name=intf,
            rate=profile.port_rate,
            burst=params.port_burst,
            limit=profile.queue_limit,
        )
        system_logger.info(
            f"Injected incast: egress {device}:{intf} toward {params.host_name} "
            f"at {profile.port_rate} with queue limit {profile.queue_limit}."
        )

    def port_shape(self, device: str, intf: str) -> tuple[dict | None, str]:
        """Root TBF rate and queue limit on ``device:intf``, plus the tc text."""
        shape = tbf_shape(
            self.runtime.exec(device, f"tc -j qdisc show dev {intf}", timeout=10)
        )
        return shape, self.runtime.tc_show_intf(device, intf).strip()

    def verify_fault(self, params: IncastTrafficNetworkLimitationParams) -> dict:
        """Verify the shallow egress queue at line rate toward the receiver."""
        device, intf = self._egress_port(params)
        profile = self.port_profile(params)
        shape, tc_output = self.port_shape(device, intf)
        expected_rate = tc_rate_bps(profile.port_rate)
        expected_limit = tc_size_bytes(profile.queue_limit)
        queue_ok = (
            shape is not None
            and expected_rate is not None
            and expected_limit is not None
            and shape["limit_bytes"] is not None
            and abs(shape["rate_bps"] - expected_rate) <= 0.05 * expected_rate
            and abs(shape["limit_bytes"] - expected_limit) <= 1024
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=queue_ok,
            details={
                "egress": f"{device}:{intf}",
                "port_shape": shape,
                "expected_rate_bps": expected_rate,
                "expected_queue_limit_bytes": expected_limit,
                "tc_output": tc_output,
                "receiver": params.host_name,
            },
        )

    def recover_fault(self, params: IncastTrafficNetworkLimitationParams) -> dict:
        """Remove the shallow egress queue."""
        device, intf = self._egress_port(params)
        try:
            self.runtime.tc_clear_intf(device, intf)
        except Exception:  # noqa: BLE001
            pass
        shape, tc_output = self.port_shape(device, intf)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=shape is None,
            details={"egress": f"{device}:{intf}", "tc_output": tc_output},
        )
