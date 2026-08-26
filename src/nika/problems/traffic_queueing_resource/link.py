from pydantic import BaseModel, Field

from nika.problems.rca.inventory import interface_on
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

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
        host_pool = list(self.net_env.hosts or [])
        if not host_pool:
            servers = getattr(self.net_env, "servers", None) or {}
            host_pool = list(servers.get("web") or [])
        if not host_pool:
            from nika.problems.support.probe_paths import get_probe_path

            path = get_probe_path(self.scenario_name or "")
            if path is not None:
                host_pool = [h for h in (path.src_host, path.peer_host) if h]
        for h in host_pool:
            if h != params.host_name:
                od_dict.setdefault(h, {})
                od_dict[h][params.host_name] = mbps
        if od_dict:
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
