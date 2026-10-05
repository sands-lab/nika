"""Link bandwidth fault retained for the 0.1.0 benchmark cases."""

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca.inventory import interface_on


class LinkBandwidthThrottlingParams(BaseModel):
    host_name: str = Field(description="Node with the restricted egress interface.")
    intf_name: str = Field(default="eth0", description="Restricted interface.")
    rate: str = "30kbit"
    burst: str = "64kb"
    limit: str = "500kb"


class LinkBandwidthThrottling(ProblemBase):
    failure_domain = FailureDomain.TRAFFIC_QUEUEING_RESOURCE
    root_cause_name = "link_bandwidth_throttling"
    description = "An egress link is limited below its expected bandwidth."
    TAGS = ["link"]
    Params = LinkBandwidthThrottlingParams

    def root_cause_resources(self, params: LinkBandwidthThrottlingParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: LinkBandwidthThrottlingParams):
        self.runtime.tc_set_tbf(
            params.host_name,
            params.intf_name,
            rate=params.rate,
            burst=params.burst,
            limit=params.limit,
        )

    def verify_fault(self, params: LinkBandwidthThrottlingParams) -> dict:
        present = self.runtime.tc_qdisc_contains(
            params.host_name, params.intf_name, "tbf"
        )
        return build_verify_result(
            self.root_cause_name,
            present,
            {"host": params.host_name, "interface": params.intf_name},
        )

    def recover_fault(self, params: LinkBandwidthThrottlingParams) -> dict:
        self.runtime.tc_clear_intf(params.host_name, params.intf_name)
        present = self.runtime.tc_qdisc_contains(
            params.host_name, params.intf_name, "tbf"
        )
        return build_verify_result(
            self.root_cause_name,
            not present,
            {"host": params.host_name, "interface": params.intf_name},
        )
