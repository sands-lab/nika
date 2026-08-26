from pydantic import BaseModel, Field

from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.topology_inventory import interface_on
from nika.runtime.base import RuntimeCapabilityError
from nika.service.containerlab.host_tc import HostTcController
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from nika.utils.logger import system_logger


def _default_link_intf(backend: str) -> str:
    return "e1-1" if backend == "containerlab" else "eth0"


def _resolve_link_intf(params_intf: str, backend: str) -> str:
    if params_intf != "eth0":
        return params_intf
    return _default_link_intf(backend)


# ==================================================================
# Problem: Link failure through controller-owned link endpoints
# ==================================================================


class LinkFailureParams(BaseModel):
    """Parameters for injecting a link-down fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")


class LinkFailure(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_down"
    TAGS: str = ["link"]

    Params = LinkFailureParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkFailureParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: LinkFailureParams):
        match self.lab_backend:
            case "kathara":
                self._inject_link_down_kathara(params)
            case "containerlab":
                self._inject_link_down_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_link_down_kathara(self, params: LinkFailureParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        self.faulty_intf = intf
        controller = KatharaVdeFaultProxy(self.runtime)
        self._proxy = controller.insert(params.host_name, intf)
        controller.set_netem_loss(self._proxy, 100)

    def _inject_link_down_containerlab(self, params: LinkFailureParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self.faulty_intf = intf
        self._host_veth = HostTcController(self.runtime).set_netem_loss(
            params.host_name, intf, 100
        )

    def verify_fault(self, params: LinkFailureParams) -> dict:
        """Verify the controller-side loss rule that models the link-down state."""
        match self.lab_backend:
            case "kathara":
                return self._verify_link_down_kathara(params)
            case "containerlab":
                return self._verify_link_down_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_link_down_kathara(self, params: LinkFailureParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        controller = KatharaVdeFaultProxy(self.runtime)
        proxy = getattr(self, "_proxy", None) or controller.discover(
            params.host_name, intf
        )
        return self._verify_link_down(
            params, intf, proxy is not None and controller.netem_configured(proxy)
        )

    def _verify_link_down_containerlab(self, params: LinkFailureParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        controller = HostTcController(self.runtime)
        peer = getattr(self, "_host_veth", None) or controller.peer_name(
            params.host_name, intf
        )
        qdisc = controller.qdisc(peer).lower()
        verified = "netem" in qdisc and "loss 100%" in qdisc
        return self._verify_link_down(params, intf, verified)

    def _verify_link_down(
        self, params: LinkFailureParams, intf: str, verified: bool
    ) -> dict:
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "intf": intf,
            },
        )

    def recover_fault(self, params: LinkFailureParams) -> dict:
        """Restore the controller-owned link endpoint."""
        intf = _resolve_link_intf(params.intf_name, self.lab_backend)
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


# ==========================================
# Problem: Link flapping from controller-owned link endpoints
# ==========================================


class LinkFlapParams(BaseModel):
    """Parameters for injecting a link-flap fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")
    down_time: int = Field(default=1, description="Down duration in seconds.")
    up_time: int = Field(default=1, description="Up duration in seconds.")


class LinkFlap(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_flap"
    TAGS: str = ["link"]

    Params = LinkFlapParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkFlapParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: LinkFlapParams):
        match self.lab_backend:
            case "kathara":
                self._inject_link_flap_kathara(params)
            case "containerlab":
                self._inject_link_flap_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_link_flap_kathara(self, params: LinkFlapParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        self._inject_link_flap(params, intf, backend="kathara")

    def _inject_link_flap_containerlab(self, params: LinkFlapParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self._inject_link_flap(params, intf, backend="containerlab")

    def _inject_link_flap(
        self, params: LinkFlapParams, intf_name: str, *, backend: str
    ) -> None:
        self.faulty_intf = intf_name
        if params.down_time <= 0 or params.up_time <= 0:
            raise ValueError("down_time and up_time must be positive integers")
        if backend == "kathara":
            controller = KatharaVdeFaultProxy(self.runtime)
            self._proxy = controller.insert(params.host_name, intf_name)
            controller.start_link_flap(self._proxy, params.down_time, params.up_time)
        else:
            controller = HostTcController(self.runtime)
            self._controller_target = controller.start_node_link_flap(
                params.host_name, intf_name, params.down_time, params.up_time
            )
        system_logger.info(
            f"Injected link flap on {params.host_name}:{intf_name} "
            f"(down_time={params.down_time}, up_time={params.up_time})"
        )

    def verify_fault(self, params: LinkFlapParams) -> dict:
        """Verify controller-side flap state without exposing it to lab nodes."""
        match self.lab_backend:
            case "kathara":
                return self._verify_link_flap_kathara(params)
            case "containerlab":
                return self._verify_link_flap_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_link_flap_kathara(self, params: LinkFlapParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        return self._verify_link_flap(params, intf)

    def _verify_link_flap_containerlab(self, params: LinkFlapParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        return self._verify_link_flap(params, intf)

    def _verify_link_flap(self, params: LinkFlapParams, intf_name: str) -> dict:
        if self.lab_backend == "kathara":
            controller = KatharaVdeFaultProxy(self.runtime)
            proxy = getattr(self, "_proxy", None) or controller.discover(
                params.host_name, intf_name
            )
            running = proxy is not None and controller.link_flap_running(proxy)
        else:
            controller = HostTcController(self.runtime)
            target = getattr(self, "_controller_target", None) or (
                f"{params.host_name}:{intf_name}"
            )
            running = controller.link_flap_running(target)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=running,
            details={
                "host": params.host_name,
                "intf": intf_name,
            },
        )

    def recover_fault(self, params: LinkFlapParams) -> dict:
        """Stop controller-side flapping and restore the logical link."""
        intf = _resolve_link_intf(params.intf_name, self.lab_backend)
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
            target = getattr(self, "_controller_target", None) or (
                f"{params.host_name}:{intf}"
            )
            controller.stop_node_link_flap(params.host_name, intf)
            restored = not controller.link_flap_running(target)
        return {
            "verified": restored,
            "details": {"host": params.host_name, "intf": intf},
        }


# ==========================================
# Problem: Link detached.
# ==========================================


class LinkDetachParams(BaseModel):
    """Parameters for injecting a link-detach fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")


class LinkDetach(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_detach"
    TAGS: str = ["link"]

    Params = LinkDetachParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkDetachParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def inject_fault(self, params: LinkDetachParams):
        match self.lab_backend:
            case "kathara":
                self._inject_link_detach_kathara(params)
            case "containerlab":
                self._inject_link_detach_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_link_detach_kathara(self, params: LinkDetachParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        self._inject_link_detach(params, intf)

    def _inject_link_detach_containerlab(self, params: LinkDetachParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self._inject_link_detach(params, intf)

    def _inject_link_detach(self, params: LinkDetachParams, intf_name: str) -> None:
        self.faulty_intf = intf_name
        self.runtime.exec(params.host_name, f"ip link del {intf_name}")
        system_logger.info(f"Injected link detach on {params.host_name}:{intf_name}")

    def verify_fault(self, params: LinkDetachParams) -> dict:
        """Verify the link-detach fault is active by confirming the interface no longer exists."""
        match self.lab_backend:
            case "kathara":
                return self._verify_link_detach_kathara(params)
            case "containerlab":
                return self._verify_link_detach_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_link_detach_kathara(self, params: LinkDetachParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        return self._verify_link_detach(params, intf)

    def _verify_link_detach_containerlab(self, params: LinkDetachParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        return self._verify_link_detach(params, intf)

    def _verify_link_detach(self, params: LinkDetachParams, intf_name: str) -> dict:
        detached = not self.runtime.interface_exists(params.host_name, intf_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=detached,
            details={
                "host": params.host_name,
                "intf": intf_name,
                "interface_exists": not detached,
            },
        )
