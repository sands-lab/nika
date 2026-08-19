from pydantic import BaseModel, Field

from nika.problems.topology_inventory import interface_on, select_host_interface
from nika.problems.problem_base import (
    FailureCause,
    FailureDomain,
    FailureImpact,
    FailureScope,
    FailureSymptom,
    FailureTemporal,
    build_verify_result,
    ProblemBase,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

# ==================================================================
# Problem: High link packet corruption between devices causing performance degradation.
# ==================================================================


# ==================================================================
# Problem: Bandwidth throttling on a link causing performance degradation.
# ==================================================================


class LinkBandwidthThrottlingParams(BaseModel):
    """Parameters for injecting a bandwidth throttling fault."""

    host_name: str = Field(description="Target host name.")
    rate: str = Field(default="30kbit", description="Bandwidth rate.")
    burst: str = Field(default="64kb", description="TBF burst.")
    limit: str = Field(default="500kb", description="TBF limit.")


class LinkBandwidthThrottling(ProblemBase):
    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    cause = FailureCause.RESOURCE
    symptom = FailureSymptom.DEGRADED_THROUGHPUT
    scope = FailureScope.LINK
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "link_bandwidth_throttling"
    TAGS: str = ["link"]

    Params = LinkBandwidthThrottlingParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.scenario_name = scenario_name

    def root_cause_resources(self, params: LinkBandwidthThrottlingParams):
        intf = select_host_interface(self.net_env, params.host_name, last=False)
        return [interface_on(self.net_env, params.host_name, intf)]

    def inject_fault(self, params: LinkBandwidthThrottlingParams):
        intf_name = self._target_intf(params.host_name, last=False)
        self.runtime.tc_set_tbf(
            params.host_name,
            intf_name,
            rate=params.rate,
            burst=params.burst,
            limit=params.limit,
        )
        od_dict: dict[str, dict[str, int]] = {}
        mbps = 20
        host_names = getattr(self.net_env, "hosts", None) or [
            node
            for node in self.runtime.list_nodes()
            if any(key in node for key in ("client", "pc", "host"))
        ]
        for h in host_names:
            if h != params.host_name:
                od_dict.setdefault(h, {})
                od_dict[h][params.host_name] = mbps
        if od_dict and self.lab_backend == "kathara":
            labels = self.runtime.start_background_od_traffic(
                od_dict, interval=300, unit="M", udp=True
            )
            system_logger.info(
                f"Started background traffic generation {labels} to amplify the bandwidth throttling effect."
            )

    def verify_fault(self, params: LinkBandwidthThrottlingParams) -> dict:
        """Verify tc qdisc on the host's first interface has TBF (token bucket filter) configured."""
        intf = self._target_intf(params.host_name, last=False)
        verified = self.runtime.tc_qdisc_contains(params.host_name, intf, "tbf")
        tc_output = self.runtime.tc_show_intf(params.host_name, intf).strip()
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "intf": intf, "tc_output": tc_output},
        )

    def _target_intf(self, host_name: str, *, last: bool) -> str:
        match self.lab_backend:
            case "containerlab":
                return "e1-1"
            case "kathara":
                interfaces = self.runtime.get_host_interfaces(host_name)
                return interfaces[-1] if last else interfaces[0]
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__}: unsupported backend {backend!r}."
                )


# ==================================================================
# Problem: incast traffic causing performance degradation.
# ==================================================================


class IncastTrafficNetworkLimitationParams(BaseModel):
    """Parameters for injecting an incast traffic network limitation fault."""

    host_name: str = Field(description="Target web server host name.")
    rate: str = Field(default="1mbit", description="Bandwidth rate.")
    burst: str = Field(default="500kb", description="TBF burst.")
    limit: str = Field(default="500kb", description="TBF limit.")
    delay_ms: int = Field(default=20, description="Netem delay milliseconds.")


class IncastTrafficNetworkLimitation(ProblemBase):
    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    cause = FailureCause.RESOURCE
    symptom = FailureSymptom.DEGRADED_THROUGHPUT
    scope = FailureScope.MULTI_NODE
    temporal = FailureTemporal.PERSISTENT
    impact = FailureImpact.PARTIAL
    root_cause_name: str = "incast_traffic_network_limitation"
    TAGS: str = ["http"]

    Params = IncastTrafficNetworkLimitationParams

    def __init__(self, scenario_name: str = "dc_clos", **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.scenario_name = scenario_name

    def root_cause_resources(self, params: IncastTrafficNetworkLimitationParams):
        return [interface_on(self.net_env, params.host_name, "eth0")]

    def inject_fault(self, params: IncastTrafficNetworkLimitationParams):
        self.runtime.tc_set_netem(
            host_name=params.host_name,
            intf_name="eth0",
            delay_ms=params.delay_ms,
            handle="1",
        )
        self.runtime.tc_set_tbf(
            host_name=params.host_name,
            intf_name="eth0",
            rate=params.rate,
            burst=params.burst,
            limit=params.limit,
            handle="10",
            parent="1:1",
        )
        system_logger.info(
            f"Injected network limitation on params.host_name {params.host_name}"
        )
        od_dict: dict[str, dict[str, int]] = {}
        mbps = 20
        for h in self.net_env.hosts:
            if h != params.host_name:
                od_dict.setdefault(h, {})
                od_dict[h][params.host_name] = mbps
        labels = self.runtime.start_background_od_traffic(
            od_dict, interval=300, unit="M", udp=True
        )
        system_logger.info(
            f"Started background traffic generation {labels} to amplify the network limitation effect."
        )

    def verify_fault(self, params: IncastTrafficNetworkLimitationParams) -> dict:
        """Verify tc qdisc on eth0 has netem or tbf (incast network limitation)."""
        tc_output = self.runtime.tc_show_intf(params.host_name, "eth0").strip()
        verified = self.runtime.tc_qdisc_contains(
            params.host_name, "eth0", "netem"
        ) or self.runtime.tc_qdisc_contains(params.host_name, "eth0", "tbf")
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "tc_output": tc_output},
        )
