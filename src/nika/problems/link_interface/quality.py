"""Link quality failure implementations."""

from pydantic import BaseModel, Field

from nika.problems.topology_inventory import interface_on, select_host_interface

from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)

from nika.runtime.base import RuntimeCapabilityError
from nika.service.containerlab.host_tc import HostTcController
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy


class LinkPacketCorruptionParams(BaseModel):
    """Parameters for injecting a high packet corruption fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str | None = Field(default=None, description="Target interface.")
    corruption_percentage: int = Field(default=60, description="Corruption percentage.")


class LinkPacketCorruption(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_packet_corruption"
    TAGS: str = ["link"]

    Params = LinkPacketCorruptionParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: LinkPacketCorruptionParams):
        intf = params.intf_name or select_host_interface(
            self.net_env, params.host_name, last=True
        )
        return [interface_on(self.net_env, params.host_name, intf)]

    def inject_fault(self, params: LinkPacketCorruptionParams):
        intf_name = self._target_intf(params.host_name, params.intf_name, last=True)
        if self.lab_backend == "kathara":
            self._proxy = KatharaVdeFaultProxy(self.runtime).insert(
                params.host_name, intf_name
            )
            KatharaVdeFaultProxy(self.runtime).set_netem_corrupt(
                self._proxy, params.corruption_percentage
            )
        else:
            self._host_veth = HostTcController(self.runtime).set_netem_corrupt(
                params.host_name, intf_name, params.corruption_percentage
            )

    def verify_fault(self, params: LinkPacketCorruptionParams) -> dict:
        """Verify the controller-side qdisc without exposing it to lab nodes."""
        intf = self._target_intf(params.host_name, params.intf_name, last=True)
        if self.lab_backend == "kathara":
            proxy = getattr(self, "_proxy", None) or KatharaVdeFaultProxy(
                self.runtime
            ).discover(params.host_name, intf)
            verified = proxy is not None and KatharaVdeFaultProxy(
                self.runtime
            ).netem_configured(proxy)
        else:
            controller = HostTcController(self.runtime)
            peer = getattr(self, "_host_veth", None) or controller.peer_name(
                params.host_name, intf
            )
            tc_output = controller.qdisc(peer).strip()
            verified = "netem" in tc_output.lower() and "corrupt" in tc_output.lower()
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "intf": intf},
        )

    def recover_fault(self, params: LinkPacketCorruptionParams) -> dict:
        """Remove controller-only injection and restore the logical link."""
        intf = self._target_intf(params.host_name, params.intf_name, last=True)
        if self.lab_backend == "kathara":
            controller = KatharaVdeFaultProxy(self.runtime)
            proxy = getattr(self, "_proxy", None) or controller.discover(
                params.host_name, intf
            )
            if proxy is not None:
                controller.remove(proxy)
            restored = (
                proxy is None or controller.discover(params.host_name, intf) is None
            )
        else:
            controller = HostTcController(self.runtime)
            peer = getattr(self, "_host_veth", None) or controller.peer_name(
                params.host_name, intf
            )
            controller.clear(peer)
            restored = "netem" not in controller.qdisc(peer).lower()
        return {
            "verified": restored,
            "details": {"host": params.host_name, "intf": intf},
        }

    def _target_intf(self, host_name: str, requested: str | None, *, last: bool) -> str:
        if requested:
            return requested
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
