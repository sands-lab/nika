"""Link quality failure implementations."""

from pydantic import BaseModel, Field

from nika.problems.topology_inventory import interface_on, select_host_interface

from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)

from nika.runtime.base import RuntimeCapabilityError


class LinkHighPacketCorruptionParams(BaseModel):
    """Parameters for injecting a high packet corruption fault."""

    host_name: str = Field(description="Target host name.")
    corruption_percentage: int = Field(default=60, description="Corruption percentage.")


class LinkHighPacketCorruption(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_high_packet_corruption"
    TAGS: str = ["link"]

    Params = LinkHighPacketCorruptionParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: LinkHighPacketCorruptionParams):
        intf = select_host_interface(self.net_env, params.host_name, last=True)
        return [interface_on(self.net_env, params.host_name, intf)]

    def inject_fault(self, params: LinkHighPacketCorruptionParams):
        intf_name = self._target_intf(params.host_name, last=True)
        self.runtime.tc_set_netem(
            params.host_name,
            intf_name,
            corrupt=params.corruption_percentage,
        )

    def verify_fault(self, params: LinkHighPacketCorruptionParams) -> dict:
        """Verify tc qdisc on the host's last interface has corruption configured."""
        intf = self._target_intf(params.host_name, last=True)
        verified = self.runtime.tc_qdisc_contains(params.host_name, intf, "corrupt")
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
