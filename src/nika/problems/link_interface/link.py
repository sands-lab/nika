import hashlib

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.rca.inventory import (
    interface_on,
    iter_link_termination_points,
    link_containing_endpoint,
    parse_endpoint,
)
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
    description = "Carrier or operational link is down on the selected attachment."
    TAGS: str = ["link"]
    supported_backends = ("kathara", "containerlab")

    Params = LinkFailureParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkFailureParams):
        return [
            link_containing_endpoint(self.net_env, params.host_name, params.intf_name)
        ]

    def _peer_endpoint(self, host: str, intf: str) -> tuple[str, str] | None:
        needle = f"{host}:{intf}"
        for _key, tps in iter_link_termination_points(self.net_env):
            endpoints = [str(ep) for ep in tps]
            if needle not in endpoints or len(endpoints) != 2:
                continue
            other = endpoints[0] if endpoints[1] == needle else endpoints[1]
            peer_host, peer_intf = parse_endpoint(other)
            if peer_host and peer_intf:
                return peer_host, peer_intf
        return None

    def _set_link_operational_down(self, host: str, intf: str) -> None:
        self.runtime.set_interface_state(host, intf, "down")

    def _set_link_operational_up(self, host: str, intf: str) -> None:
        self.runtime.set_interface_state(host, intf, "up")

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
        self._set_link_operational_down(params.host_name, intf)
        self._link_down_peer = self._peer_endpoint(params.host_name, intf)
        if self._link_down_peer is not None:
            peer_host, peer_intf = self._link_down_peer
            self._set_link_operational_down(peer_host, peer_intf)

    def _inject_link_down_containerlab(self, params: LinkFailureParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self.faulty_intf = intf
        controller = HostTcController(self.runtime)
        self._link_down_mode, self._link_down_target = controller.set_link_down(
            params.host_name, intf
        )

    def verify_fault(self, params: LinkFailureParams) -> dict:
        """Verify the attachment reports operational link down."""
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
        operstate = self.runtime.get_interface_operstate(params.host_name, intf)
        peer = getattr(self, "_link_down_peer", None) or self._peer_endpoint(
            params.host_name, intf
        )
        peer_ok = True
        if peer is not None:
            peer_ok = self.runtime.get_interface_operstate(peer[0], peer[1]) == "down"
        return self._verify_link_down(
            params, intf, operstate, operstate == "down" and peer_ok
        )

    def _verify_link_down_containerlab(self, params: LinkFailureParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        controller = HostTcController(self.runtime)
        operstate = self.runtime.get_interface_operstate(params.host_name, intf)
        mode = getattr(self, "_link_down_mode", None)
        target = getattr(self, "_link_down_target", None)
        if mode == "host_peer" and target:
            artifact_ok = controller.link_peer_down(target)
        elif mode == "node_intf":
            artifact_ok = operstate == "down"
        else:
            try:
                peer = controller.peer_name(params.host_name, intf)
                artifact_ok = controller.link_peer_down(peer)
            except RuntimeCapabilityError:
                artifact_ok = operstate == "down"
        return self._verify_link_down(params, intf, operstate, artifact_ok)

    def _verify_link_down(
        self,
        params: LinkFailureParams,
        intf: str,
        operstate: str,
        artifact_ok: bool,
    ) -> dict:
        verified = operstate == "down" and artifact_ok
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "intf": intf,
                "operstate": operstate,
            },
        )

    def recover_fault(self, params: LinkFailureParams) -> dict:
        """Restore carrier on the selected attachment."""
        intf = _resolve_link_intf(params.intf_name, self.lab_backend)
        if self.lab_backend == "kathara":
            self._set_link_operational_up(params.host_name, intf)
            peer = getattr(self, "_link_down_peer", None) or self._peer_endpoint(
                params.host_name, intf
            )
            if peer is not None:
                self._set_link_operational_up(peer[0], peer[1])
            restored = (
                self.runtime.get_interface_operstate(params.host_name, intf) == "up"
            )
        else:
            controller = HostTcController(self.runtime)
            mode = getattr(self, "_link_down_mode", None)
            target = getattr(self, "_link_down_target", None)
            if mode == "host_peer" and target:
                controller._run("ip", "link", "set", "dev", target, "up")
            elif mode == "node_intf":
                controller.set_node_link_up(params.host_name, intf)
            else:
                try:
                    peer = controller.peer_name(params.host_name, intf)
                    controller._run("ip", "link", "set", "dev", peer, "up")
                except RuntimeCapabilityError:
                    controller.set_node_link_up(params.host_name, intf)
            operstate = self.runtime.get_interface_operstate(params.host_name, intf)
            restored = operstate == "up"
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
    probe_dst_ip: str | None = Field(
        default=None,
        description="ICMP-reachable destination for flap symptom probes.",
    )
    observer_device: str | None = Field(
        default=None,
        description="Optional probe source host for path symptom checks.",
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional probe source override (ISP stub hosts).",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer host for cross-subnet probe resolution.",
    )


class LinkFlap(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_flap"
    description = "Logical link flaps between up and down."
    TAGS: str = ["link"]
    supported_backends = ("kathara", "containerlab")

    Params = LinkFlapParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkFlapParams):
        return [
            link_containing_endpoint(self.net_env, params.host_name, params.intf_name)
        ]

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
# Problem: Link capacity bottleneck (controller-side TBF)
# ==========================================


class LinkCapacityBottleneckParams(BaseModel):
    """Parameters for injecting a link capacity bottleneck fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")
    rate: str = Field(default="200kbit", description="Bandwidth rate.")
    burst: str = Field(default="64kb", description="TBF burst.")
    limit: str = Field(default="500kb", description="TBF limit.")
    probe_dst_ip: str | None = Field(
        default=None,
        description="Optional ICMP/iperf destination for symptom probes.",
    )
    observer_device: str | None = Field(
        default=None,
        description="Optional probe source host for path symptom checks.",
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional probe source override (ISP stub hosts).",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer host for cross-subnet probe resolution.",
    )


class LinkCapacityBottleneck(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_capacity_bottleneck"
    description = "Logical link capacity is bottlenecked below demand."
    TAGS: str = ["link"]
    supported_backends = ("kathara",)

    Params = LinkCapacityBottleneckParams

    symptom_desc = "Users report slow throughput across a link."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"

    def root_cause_resources(self, params: LinkCapacityBottleneckParams):
        return [
            link_containing_endpoint(self.net_env, params.host_name, params.intf_name)
        ]

    def inject_fault(self, params: LinkCapacityBottleneckParams):
        match self.lab_backend:
            case "kathara":
                self._inject_capacity_kathara(params)
            case "containerlab":
                self._inject_capacity_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def _inject_capacity_kathara(self, params: LinkCapacityBottleneckParams) -> None:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        self.faulty_intf = intf
        controller = KatharaVdeFaultProxy(self.runtime)
        self._proxy = controller.insert(params.host_name, intf)
        controller.set_tbf(
            self._proxy, rate=params.rate, burst=params.burst, limit=params.limit
        )

    def _inject_capacity_containerlab(
        self, params: LinkCapacityBottleneckParams
    ) -> None:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        self.faulty_intf = intf
        self._host_veth = HostTcController(self.runtime).set_tbf(
            params.host_name,
            intf,
            rate=params.rate,
            burst=params.burst,
            limit=params.limit,
        )

    def verify_fault(self, params: LinkCapacityBottleneckParams) -> dict:
        """Verify the controller-side TBF without exposing it to lab nodes."""
        match self.lab_backend:
            case "kathara":
                return self._verify_capacity_kathara(params)
            case "containerlab":
                return self._verify_capacity_containerlab(params)
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )

    def _verify_capacity_kathara(self, params: LinkCapacityBottleneckParams) -> dict:
        intf = _resolve_link_intf(params.intf_name, "kathara")
        controller = KatharaVdeFaultProxy(self.runtime)
        proxy = getattr(self, "_proxy", None) or controller.discover(
            params.host_name, intf
        )
        verified = proxy is not None and controller.tbf_configured(proxy)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "intf": intf},
        )

    def _verify_capacity_containerlab(
        self, params: LinkCapacityBottleneckParams
    ) -> dict:
        intf = _resolve_link_intf(params.intf_name, "containerlab")
        controller = HostTcController(self.runtime)
        peer = getattr(self, "_host_veth", None) or controller.peer_name(
            params.host_name, intf
        )
        verified = "tbf" in controller.qdisc(peer).lower()
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "intf": intf},
        )

    def recover_fault(self, params: LinkCapacityBottleneckParams) -> dict:
        """Remove controller-side capacity limiting and restore the logical link."""
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
            restored = "tbf" not in controller.qdisc(peer).lower()
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
    probe_dst_ip: str | None = Field(
        default=None,
        description="ICMP-reachable destination for detach symptom probes.",
    )
    observer_device: str | None = Field(
        default=None,
        description="Optional probe source host for path symptom checks.",
    )
    symptom_host: str | None = Field(
        default=None,
        description="Optional probe source override (ISP stub hosts).",
    )
    peer_host: str | None = Field(
        default=None,
        description="Optional peer host for cross-subnet probe resolution.",
    )


class LinkDetach(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name: str = "link_detach"
    description = "Network attachment is detached; the interface is gone from the node."
    TAGS: str = ["link"]
    supported_backends = ("kathara", "containerlab")

    Params = LinkDetachParams

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"
        self._detach_netns: str | None = None

    def root_cause_resources(self, params: LinkDetachParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    @staticmethod
    def _detach_netns_name(lab_name: str, host: str, intf: str) -> str:
        key = hashlib.blake2s(
            f"{lab_name}:{host}:{intf}".encode(),
            digest_size=8,
        ).hexdigest()
        return f"nika-detach-{key}"

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
        """Move the attachment into a private netns so it disappears from inventory."""
        self.faulty_intf = intf_name
        netns = self._detach_netns_name(
            self.runtime.lab_name, params.host_name, intf_name
        )
        self._detach_netns = netns
        host = params.host_name
        self.runtime.exec(host, f"ip netns del {netns} 2>/dev/null || true")
        self.runtime.exec(host, f"ip netns add {netns}")
        self.runtime.exec(host, f"ip link set dev {intf_name} netns {netns}")
        system_logger.info(
            f"Injected link detach on {host}:{intf_name} (moved to netns {netns})"
        )

    def verify_fault(self, params: LinkDetachParams) -> dict:
        """Verify the interface is gone and the default probe path is unreachable."""
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

    def _probe_observer(
        self, params: LinkDetachParams
    ) -> tuple[str | None, str | None]:
        from nika.problems.support.probe_paths import get_probe_path

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

    def _light_symptom_unreachable(self, params: LinkDetachParams) -> tuple[bool, dict]:
        observer, dst_ip = self._probe_observer(params)
        if observer is None or dst_ip is None:
            return True, {"skipped": True, "reason": "no_probe_path"}
        ping_ok = self.runtime.ping_ok(observer, dst_ip, count=3)
        return not ping_ok, {
            "observer": observer,
            "dst_ip": dst_ip,
            "ping_ok": ping_ok,
        }

    def _verify_link_detach(self, params: LinkDetachParams, intf_name: str) -> dict:
        interface_gone = not self.runtime.interface_exists(params.host_name, intf_name)
        symptom_ok, symptom_details = self._light_symptom_unreachable(params)
        verified = interface_gone and symptom_ok
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "artifact": {
                    "verified": interface_gone,
                    "host": params.host_name,
                    "intf": intf_name,
                    "interface_exists": not interface_gone,
                },
                "symptom": {
                    "verified": symptom_ok,
                    **symptom_details,
                },
            },
        )

    def recover_fault(self, params: LinkDetachParams) -> dict:
        """Move the detached interface back into the node namespace."""
        intf = _resolve_link_intf(params.intf_name, self.lab_backend)
        netns = self._detach_netns or self._detach_netns_name(
            self.runtime.lab_name, params.host_name, intf
        )
        host = params.host_name
        self.runtime.exec(
            host,
            f"ip netns exec {netns} ip link set dev {intf} netns 1 2>/dev/null || true",
        )
        self.runtime.exec(host, f"ip link set dev {intf} up 2>/dev/null || true")
        self.runtime.exec(host, f"ip netns del {netns} 2>/dev/null || true")
        self._detach_netns = None
        restored = self.runtime.interface_exists(host, intf)
        return {
            "verified": restored,
            "details": {"host": host, "intf": intf, "interface_exists": restored},
        }
