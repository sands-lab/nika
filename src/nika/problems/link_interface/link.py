import re
import time

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.rca.inventory import (
    interface_on,
    iter_link_termination_points,
    link_containing_endpoint,
    parse_endpoint,
    resolve_default_intf,
)
from nika.problems.support.benchmark_targets import (
    endpoint_target,
    isp_link_target,
    resolve_link_flap_params,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from nika.service.containerlab.host_tc import HostTcController
from nika.utils.logger import system_logger


_DAEMON_ROUTE_RE = re.compile(r"\bproto (kernel|bgp|ospf|isis|rip|zebra|dhcp)\b")


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
    RECORDED_ATTRS = ("faulty_intf",)

    Params = LinkFailureParams

    BENCHMARK_TARGETS = "link"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            return isp_link_target(ctx)
        return endpoint_target(ctx, link=True)

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
        intf = resolve_default_intf(params.intf_name, self.net_env)
        self.faulty_intf = intf
        self._set_link_operational_down(params.host_name, intf)
        self._link_down_peer = self._peer_endpoint(params.host_name, intf)
        if self._link_down_peer is not None:
            peer_host, peer_intf = self._link_down_peer
            self._set_link_operational_down(peer_host, peer_intf)

    def _inject_link_down_containerlab(self, params: LinkFailureParams) -> None:
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
    RECORDED_ATTRS = ("faulty_intf",)

    Params = LinkFlapParams

    BENCHMARK_TARGETS = "link"
    BENCHMARK_POINT_TO_POINT = True

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            params = isp_link_target(ctx)
            params["down_time"] = "1"
            params["up_time"] = "1"
            return params
        return resolve_link_flap_params(
            ctx.scenario,
            ctx.net_env,
            ctx.rng,
            ctx.backend,
            host_pool=ctx.host_pool,
            host0=ctx.host0,
        )

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
        intf = resolve_default_intf(params.intf_name, self.net_env)
        self._inject_link_flap(params, intf, backend="kathara")

    def _inject_link_flap_containerlab(self, params: LinkFlapParams) -> None:
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
        intf = resolve_default_intf(params.intf_name, self.net_env)
        return self._verify_link_flap(params, intf)

    def _verify_link_flap_containerlab(self, params: LinkFlapParams) -> dict:
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
    supported_backends = ("kathara", "containerlab")
    RECORDED_ATTRS = ("faulty_intf",)

    Params = LinkCapacityBottleneckParams

    BENCHMARK_TARGETS = "link"
    BENCHMARK_POINT_TO_POINT = True

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            params = isp_link_target(ctx)
            rate = "30kbit"
        else:
            params = endpoint_target(ctx, link=True)
            rate = (
                "10kbit"
                if ctx.scenario in {"enterprise_branch", "sdn_l3_clos", "p4_dc_fabric"}
                else "30kbit"
            )
        params["rate"] = rate
        params["burst"] = "64kb"
        params["limit"] = "500kb"
        return params

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
        intf = resolve_default_intf(params.intf_name, self.net_env)
        self.faulty_intf = intf
        controller = KatharaVdeFaultProxy(self.runtime)
        self._proxy = controller.insert(params.host_name, intf)
        controller.set_tbf(
            self._proxy, rate=params.rate, burst=params.burst, limit=params.limit
        )

    def _inject_capacity_containerlab(
        self, params: LinkCapacityBottleneckParams
    ) -> None:
        intf = resolve_default_intf(params.intf_name, self.net_env)
        self.faulty_intf = intf
        controller = HostTcController(self.runtime)
        peer = controller.set_tbf(
            params.host_name,
            intf,
            rate=params.rate,
            burst=params.burst,
            limit=params.limit,
        )
        if peer.startswith("node:"):
            self._host_veth = None
            self._capacity_mode = "node_intf"
        else:
            self._host_veth = peer
            self._capacity_mode = "host_peer"

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
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
        intf = resolve_default_intf(params.intf_name, self.net_env)
        mode = getattr(self, "_capacity_mode", None)
        if mode == "node_intf":
            verified = self.runtime.tc_qdisc_contains(params.host_name, intf, "tbf")
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=verified,
                details={
                    "host": params.host_name,
                    "intf": intf,
                    "mode": "node_intf",
                },
            )
        if mode is None and getattr(self, "_host_veth", None) is None:
            try:
                controller = HostTcController(self.runtime)
                controller.peer_name(params.host_name, intf)
            except RuntimeCapabilityError:
                verified = self.runtime.tc_qdisc_contains(params.host_name, intf, "tbf")
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "intf": intf,
                        "mode": "node_intf",
                    },
                )
        controller = HostTcController(self.runtime)
        peer = getattr(self, "_host_veth", None) or controller.peer_name(
            params.host_name, intf
        )
        verified = "tbf" in controller.qdisc(peer).lower()
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "intf": intf, "mode": "host_peer"},
        )

    def recover_fault(self, params: LinkCapacityBottleneckParams) -> dict:
        """Remove controller-side capacity limiting and restore the logical link."""
        intf = resolve_default_intf(params.intf_name, self.net_env)
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
            mode = getattr(self, "_capacity_mode", None)
            if mode == "node_intf":
                self.runtime.tc_clear_intf(params.host_name, intf)
                restored = not self.runtime.tc_qdisc_contains(
                    params.host_name, intf, "tbf"
                )
            else:
                controller = HostTcController(self.runtime)
                try:
                    peer = getattr(self, "_host_veth", None) or controller.peer_name(
                        params.host_name, intf
                    )
                    controller.clear(peer)
                    restored = "tbf" not in controller.qdisc(peer).lower()
                except RuntimeCapabilityError:
                    self.runtime.tc_clear_intf(params.host_name, intf)
                    restored = not self.runtime.tc_qdisc_contains(
                        params.host_name, intf, "tbf"
                    )
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
    RECORDED_ATTRS = ("faulty_intf",)

    Params = LinkDetachParams

    BENCHMARK_TARGETS = "link"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            return isp_link_target(ctx)
        return endpoint_target(ctx, link=True, detach=True)

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        rows = super().benchmark_inject_options(ctx, base)
        if ctx.scenario == "campus_lan":
            # LB backends are off the default pc→web0 ICMP probe path.
            rows = [
                row
                for row in rows
                if not str(row.get("host_name", "")).startswith("backend_web_")
            ]
        return rows

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"
        self._detach_pid: str | None = None
        self._saved_addrs: list[str] = []
        self._saved_routes: list[str] = []

    def root_cause_resources(self, params: LinkDetachParams):
        return [interface_on(self.net_env, params.host_name, params.intf_name)]

    def _save_l3_state(self, host: str, intf: str) -> None:
        """Remember addresses and static routes; the netns move drops them.

        Routing-daemon routes are skipped because the daemons reinstall them.
        """
        addr_out = self.runtime.exec(
            host, f"ip -o addr show dev {intf} 2>/dev/null || true"
        )
        self._saved_addrs = []
        for line in addr_out.splitlines():
            fields = line.split()
            if len(fields) < 4 or fields[2] not in ("inet", "inet6"):
                continue
            if fields[3].lower().startswith("fe80:"):
                continue
            self._saved_addrs.append(fields[3])
        route_out = self.runtime.exec(
            host, f"ip -4 route show dev {intf} 2>/dev/null || true"
        )
        self._saved_routes = [
            line.strip()
            for line in route_out.splitlines()
            if line.strip() and not _DAEMON_ROUTE_RE.search(line)
        ]

    def _restore_l3_state(self, host: str, intf: str) -> None:
        for cidr in self._saved_addrs:
            self.runtime.exec(
                host, f"ip addr replace {cidr} dev {intf} 2>/dev/null || true"
            )
        for route in self._saved_routes:
            self.runtime.exec(
                host, f"ip route replace {route} dev {intf} 2>/dev/null || true"
            )
        self._saved_addrs = []
        self._saved_routes = []

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
        intf = resolve_default_intf(params.intf_name, self.net_env)
        self._inject_link_detach(params, intf)

    def _inject_link_detach_containerlab(self, params: LinkDetachParams) -> None:
        intf = resolve_default_intf(params.intf_name, self.net_env)
        self._inject_link_detach(params, intf)

    def _inject_link_detach(self, params: LinkDetachParams, intf_name: str) -> None:
        """Move the attachment into a private netns so it disappears from inventory.

        The netns is held by a background process instead of `ip netns add`,
        whose bind mount is denied by Docker's default AppArmor profile.
        """
        self.faulty_intf = intf_name
        host = params.host_name
        self._save_l3_state(host, intf_name)
        start_out = self.runtime.exec(
            host,
            "nohup unshare -n sleep infinity </dev/null >/dev/null 2>&1 & echo PID:$!",
        )
        match = re.search(r"PID:(\d+)", start_out)
        pid = match.group(1) if match else ""
        self._detach_pid = pid or None
        # The holder enters its netns only once `unshare` runs; moving the
        # interface before that is a silent no-op.
        move_out = self.runtime.exec(
            host,
            f'i=0; while [ $i -lt 50 ] && [ "$(readlink /proc/{pid}/ns/net)" = '
            f'"$(readlink /proc/self/ns/net)" ]; do sleep 0.1; i=$((i+1)); done; '
            f"ip link set dev {intf_name} netns {pid} 2>&1",
            timeout=15,
        )
        # Brief settle: some runners still list the iface until the move commits.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if not self.runtime.interface_exists(host, intf_name):
                break
            time.sleep(0.2)
        else:
            if pid:
                self.runtime.exec(host, f"kill {pid} 2>/dev/null || true")
            self._detach_pid = None
            raise RuntimeError(
                f"link_detach failed: {host}:{intf_name} still present after "
                f"moving to the netns of pid {pid or '?'}. start output={start_out!r}; "
                f"move output={move_out!r}. Containers need NET_ADMIN/SYS_ADMIN "
                "(or privileged) for `unshare -n` and `ip link set … netns`."
            )
        system_logger.info(
            f"Injected link detach on {host}:{intf_name} (moved to netns of pid {pid})"
        )

    def verify_fault(self, params: LinkDetachParams) -> dict:
        """Verify the interface is gone from the node namespace."""
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
        intf = resolve_default_intf(params.intf_name, self.net_env)
        return self._verify_link_detach(params, intf)

    def _verify_link_detach_containerlab(self, params: LinkDetachParams) -> dict:
        intf = resolve_default_intf(params.intf_name, self.net_env)
        return self._verify_link_detach(params, intf)

    def _verify_link_detach(self, params: LinkDetachParams, intf_name: str) -> dict:
        interface_gone = not self.runtime.interface_exists(params.host_name, intf_name)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=interface_gone,
            details={
                "host": params.host_name,
                "intf": intf_name,
                "interface_exists": not interface_gone,
            },
        )

    def recover_fault(self, params: LinkDetachParams) -> dict:
        """Move the detached interface back into the node namespace."""
        intf = resolve_default_intf(params.intf_name, self.net_env)
        host = params.host_name
        pid = self._detach_pid
        if pid:
            self.runtime.exec(
                host,
                f"nsenter -t {pid} -n ip link set dev {intf} netns 1 2>/dev/null || true",
            )
            self.runtime.exec(host, f"kill {pid} 2>/dev/null || true")
        self.runtime.exec(host, f"ip link set dev {intf} up 2>/dev/null || true")
        self._restore_l3_state(host, intf)
        self._detach_pid = None
        restored = self.runtime.interface_exists(host, intf)
        return {
            "verified": restored,
            "details": {"host": host, "intf": intf, "interface_exists": restored},
        }
