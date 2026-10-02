"""Link quality failure implementations."""

from pydantic import BaseModel, Field

from nika.problems.rca.inventory import (
    link_containing_endpoint,
    select_host_interface,
)

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)

from nika.problems.support.benchmark_targets import (
    isp_link_target,
    resolve_link_corruption_params,
)
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy


class LinkPacketCorruptionParams(BaseModel):
    """Parameters for injecting a low-rate packet corruption fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str | None = Field(default=None, description="Target interface.")
    corruption_percentage: int = Field(default=8, description="Corruption percentage.")
    probe_dst_ip: str | None = Field(
        default=None, description="Optional probe destination override."
    )
    observer_device: str | None = Field(
        default=None, description="Optional probe source override."
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional probe source override (ISP stub hosts).",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer host for cross-subnet probe resolution.",
    )


class LinkPacketCorruption(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_packet_corruption"
    description = "A faulty link corrupts a fraction of the frames it carries."
    TAGS: str = ["link"]
    supported_backends = ("kathara",)

    Params = LinkPacketCorruptionParams

    BENCHMARK_TARGETS = "link"
    BENCHMARK_POINT_TO_POINT = True

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            params = isp_link_target(ctx)
            params["corruption_percentage"] = "10"
            return params
        return resolve_link_corruption_params(
            ctx.scenario,
            ctx.net_env,
            ctx.rng,
            ctx.backend,
            host_pool=ctx.host_pool,
            host0=ctx.host0,
        )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: LinkPacketCorruptionParams):
        intf = params.intf_name or select_host_interface(
            self.net_env, params.host_name, last=True
        )
        return [link_containing_endpoint(self.net_env, params.host_name, intf)]

    def inject_fault(self, params: LinkPacketCorruptionParams):
        intf_name = self._target_intf(params.host_name, params.intf_name, last=True)
        controller = KatharaVdeFaultProxy(self.runtime)
        self._proxy = controller.insert(params.host_name, intf_name)
        controller.set_netem_corrupt(self._proxy, params.corruption_percentage)

    def verify_fault(self, params: LinkPacketCorruptionParams) -> dict:
        """Verify the hidden corrupt qdisc while both link ends stay up."""
        intf = self._target_intf(params.host_name, params.intf_name, last=True)
        artifact_ok, artifact_details = self._verify_artifact(params, intf)
        link_ok, link_details = self._link_up(params, intf)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=artifact_ok and link_ok,
            details={
                "artifact": {"verified": artifact_ok, **artifact_details},
                # Key kept for tests/support readers; holds link-state evidence only.
                "symptom": {"verified": link_ok, **link_details},
            },
        )

    def recover_fault(self, params: LinkPacketCorruptionParams) -> dict:
        """Remove controller-only injection and restore the logical link."""
        intf = self._target_intf(params.host_name, params.intf_name, last=True)
        controller = KatharaVdeFaultProxy(self.runtime)
        proxy = getattr(self, "_proxy", None) or controller.discover(
            params.host_name, intf
        )
        if proxy is not None:
            controller.remove(proxy)
        restored = proxy is None or controller.discover(params.host_name, intf) is None
        return {
            "verified": restored,
            "details": {"host": params.host_name, "intf": intf},
        }

    def _verify_artifact(
        self, params: LinkPacketCorruptionParams, intf: str
    ) -> tuple[bool, dict]:
        controller = KatharaVdeFaultProxy(self.runtime)
        proxy = getattr(self, "_proxy", None) or controller.discover(
            params.host_name, intf
        )
        verified = proxy is not None and controller.netem_corrupt_configured(proxy)
        return verified, {"host": params.host_name, "intf": intf}

    def _link_up(
        self, params: LinkPacketCorruptionParams, intf: str
    ) -> tuple[bool, dict]:
        host_operstate = self.runtime.get_interface_operstate(params.host_name, intf)
        peer_ep = self._link_peer_endpoint(params.host_name, intf)
        peer_operstate = (
            self.runtime.get_interface_operstate(peer_ep[0], peer_ep[1])
            if peer_ep is not None
            else "unknown"
        )
        link_up = host_operstate == "up" and (peer_ep is None or peer_operstate == "up")
        return link_up, {
            "host_operstate": host_operstate,
            "peer_operstate": peer_operstate,
            "link_up": link_up,
        }

    def _link_peer_endpoint(
        self, host_name: str, intf_name: str
    ) -> tuple[str, str] | None:
        controller = KatharaVdeFaultProxy(self.runtime)
        state = getattr(self, "_proxy", None) or controller.discover(
            host_name, intf_name
        )
        if state is None:
            return None
        if state.endpoint.node == host_name and state.endpoint.intf == intf_name:
            return state.peer.node, state.peer.intf
        return state.endpoint.node, state.endpoint.intf

    def _target_intf(self, host_name: str, requested: str | None, *, last: bool) -> str:
        if requested:
            return requested
        interfaces = self.runtime.get_host_interfaces(host_name)
        return interfaces[-1] if last else interfaces[0]
