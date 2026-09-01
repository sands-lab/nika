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

from nika.net_env.verify import ping_stats
from nika.problems.support.probe_paths import get_probe_path
from nika.runtime.base import RuntimeCapabilityError
from nika.service.containerlab.host_tc import HostTcController
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
    description = (
        "Packets on the logical link are corrupted in transit while the link "
        "stays up; applications see partial loss, TCP retransmissions, and "
        "reduced throughput."
    )
    TAGS: str = ["link"]
    supported_backends = ("kathara",)

    Params = LinkPacketCorruptionParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)

    def root_cause_resources(self, params: LinkPacketCorruptionParams):
        intf = params.intf_name or select_host_interface(
            self.net_env, params.host_name, last=True
        )
        return [link_containing_endpoint(self.net_env, params.host_name, intf)]

    def inject_fault(self, params: LinkPacketCorruptionParams):
        intf_name = self._target_intf(params.host_name, params.intf_name, last=True)
        pct = params.corruption_percentage
        if self.lab_backend == "kathara":
            controller = KatharaVdeFaultProxy(self.runtime)
            self._proxy = controller.insert(params.host_name, intf_name)
            controller.set_netem_corrupt(self._proxy, pct)
        else:
            controller = HostTcController(self.runtime)
            self._host_veth = controller.set_netem_corrupt(
                params.host_name, intf_name, pct
            )

    def verify_fault(self, params: LinkPacketCorruptionParams) -> dict:
        """Verify hidden qdisc and light partial-degradation symptoms."""
        intf = self._target_intf(params.host_name, params.intf_name, last=True)
        artifact_ok, artifact_details = self._verify_artifact(params, intf)
        symptom_ok, symptom_details = self._light_symptom(params, intf)
        verified = artifact_ok and symptom_ok
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "artifact": {"verified": artifact_ok, **artifact_details},
                "symptom": {"verified": symptom_ok, **symptom_details},
            },
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

    def _verify_artifact(
        self, params: LinkPacketCorruptionParams, intf: str
    ) -> tuple[bool, dict]:
        if self.lab_backend == "kathara":
            proxy = getattr(self, "_proxy", None) or KatharaVdeFaultProxy(
                self.runtime
            ).discover(params.host_name, intf)
            verified = proxy is not None and KatharaVdeFaultProxy(
                self.runtime
            ).netem_corrupt_configured(proxy)
        else:
            controller = HostTcController(self.runtime)
            peer = getattr(self, "_host_veth", None) or controller.peer_name(
                params.host_name, intf
            )
            tc_output = controller.qdisc(peer).strip()
            verified = "netem" in tc_output.lower() and "corrupt" in tc_output.lower()
        return verified, {"host": params.host_name, "intf": intf}

    def _probe_observer(
        self, params: LinkPacketCorruptionParams
    ) -> tuple[str | None, str | None]:
        topo_size = getattr(self.net_env, "topo_size", None) or "s"
        path = get_probe_path(self.scenario_name or "", topo_size=str(topo_size))
        observer = (
            params.observer_device
            or params.symptom_host
            or (path.src_host if path is not None else None)
        )
        dst_ip = params.probe_dst_ip or (path.dst_ip if path is not None else None)
        if not observer or not dst_ip:
            return None, None
        return observer, dst_ip

    def _light_symptom(
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

        observer, dst_ip = self._probe_observer(params)
        if observer is None or dst_ip is None:
            return link_up, {
                "skipped_ping": True,
                "host_operstate": host_operstate,
                "peer_operstate": peer_operstate,
                "link_up": link_up,
            }

        stats = ping_stats(self.runtime, observer, dst_ip, count=30, interval_sec=0.1)
        not_total_outage = stats.received > 0 and stats.loss_percent < 90.0
        verified = link_up and not_total_outage
        return verified, {
            "host_operstate": host_operstate,
            "peer_operstate": peer_operstate,
            "link_up": link_up,
            "observer": observer,
            "dst_ip": dst_ip,
            "loss_percent": stats.loss_percent,
            "received": stats.received,
            "not_total_outage": not_total_outage,
        }

    def _link_peer_endpoint(
        self, host_name: str, intf_name: str
    ) -> tuple[str, str] | None:
        if self.lab_backend == "kathara":
            controller = KatharaVdeFaultProxy(self.runtime)
            state = getattr(self, "_proxy", None) or controller.discover(
                host_name, intf_name
            )
            if state is None:
                return None
            if state.endpoint.node == host_name and state.endpoint.intf == intf_name:
                return state.peer.node, state.peer.intf
            return state.endpoint.node, state.endpoint.intf
        return None

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
