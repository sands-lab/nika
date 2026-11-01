import json
import re
import time

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.support.oracle import (
    addr_step,
    is_ip_node,
    toward,
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    iface_down,
    iface_state,
    lacks,
    shell,
)
from nika.problems.rca.inventory import (
    interface_on,
    iter_link_termination_points,
    link_containing_endpoint,
    parse_endpoint,
    resolve_default_intf,
)
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops
from nika.problems.support.benchmark_targets import (
    endpoint_target,
    isp_link_target,
    loaded_inventory,
    resolve_link_flap_params,
    unique,
)
from nika.net_env.utils.kathara.docker_files.docker_images import nika_image
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from nika.runtime.shared.containers import run_labelled_helper
from nika.service.containerlab.host_tc import HostTcController
from nika.traffic.workload import BULK_TRANSIT
from nika.utils.logger import system_logger


_SKIP_ROUTE_PROTOS = frozenset(
    {"kernel", "bgp", "ospf", "isis", "rip", "zebra", "dhcp", "ra"}
)
_SKIP_ROUTE_TYPES = frozenset({"multicast", "broadcast", "local", "nat"})
_MAIN_TABLES = frozenset({None, 254, "254", "main"})
_LOCAL_TABLES = frozenset({255, "255", "local"})
_ROUTE_RESTORE_OK = "NIKA_ROUTE_OK"


def parse_ip_route_json(raw: str) -> list[dict] | None:
    """Parse ``ip -j route`` output into route objects; ``None`` if invalid."""
    text = raw.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        return [data]
    return None


def _onlink(route: dict) -> bool:
    flags = route.get("flags") or []
    if isinstance(flags, str):
        return "onlink" in flags.lower()
    return any(str(flag).lower() == "onlink" for flag in flags)


def _link_local_gateway(gateway: object) -> bool:
    return str(gateway or "").lower().startswith("fe80:")


def _nexthops_on_intf(route: dict, intf: str) -> list[dict]:
    hops = route.get("nexthops")
    if not isinstance(hops, list):
        return []
    usable: list[dict] = []
    for hop in hops:
        if not isinstance(hop, dict):
            continue
        hop_dev = hop.get("dev")
        if hop_dev and str(hop_dev) != intf:
            continue
        if not hop.get("gateway"):
            continue
        usable.append(hop)
    return usable


def static_route_replace_command(family: str, route: dict, intf: str) -> str | None:
    """Return ``ip route replace`` for a non-daemon, non-RA route on ``intf``.

    Emits ``via`` / ``nexthop``, ``onlink``, ``src``, and ``table`` from the
    ``ip -j route`` object. Returns ``None`` when the route cannot be expressed.
    """
    proto = str(route.get("protocol") or "")
    if proto in _SKIP_ROUTE_PROTOS:
        return None
    dst = str(route.get("dst") or "")
    if not dst or dst.lower().startswith(("fe80:", "ff00:", "ff02:")):
        return None
    rtype = str(route.get("type") or "")
    if rtype in _SKIP_ROUTE_TYPES:
        return None
    table = route.get("table")
    if table in _LOCAL_TABLES:
        return None
    hops = _nexthops_on_intf(route, intf)
    raw_hops = route.get("nexthops")
    if isinstance(raw_hops, list) and raw_hops and not hops:
        return None
    gateway = route.get("gateway")
    parts = ["ip", family, "route", "replace"]
    if rtype and rtype != "unicast":
        parts.append(rtype)
    parts.append(dst)
    metric = route.get("metric")
    prefsrc = route.get("prefsrc") or route.get("src")

    def append_common() -> None:
        if metric is not None:
            parts.extend(["metric", str(metric)])
        if proto:
            parts.extend(["proto", proto])
        if prefsrc:
            parts.extend(["src", str(prefsrc)])
        if table not in _MAIN_TABLES:
            parts.extend(["table", str(table)])

    if hops:
        append_common()
        for hop in hops:
            parts.extend(["nexthop", "via", str(hop["gateway"])])
            parts.extend(["dev", str(hop.get("dev") or intf)])
            weight = hop.get("weight")
            if weight is not None:
                parts.extend(["weight", str(weight)])
            if _onlink(hop) or _link_local_gateway(hop.get("gateway")):
                parts.append("onlink")
        return " ".join(parts)
    if gateway:
        parts.extend(["via", str(gateway)])
    append_common()
    parts.extend(["dev", intf])
    if _onlink(route) or _link_local_gateway(gateway):
        parts.append("onlink")
    return " ".join(parts)


# ------------------------------------------------------------------
# Oracle helpers
# ------------------------------------------------------------------

_RATE_RE = re.compile(r"([0-9.]+)\s*([KMG]?)bits/sec\b.*\breceiver\b")
_RATE_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}


def _role(net_env, node: str) -> str | None:
    identity = (getattr(net_env, "machine_identities", None) or {}).get(node)
    return None if identity is None else getattr(identity.role, "value", identity.role)


def _single_homed(net_env, node: str) -> bool:
    """An end host: every path to or from it crosses its one data link."""
    return is_ip_node(net_env, node) and _role(net_env, node) in ("host", "service")


def _remote_host(net_env, exclude: set[str]) -> str | None:
    """An addressed end host elsewhere in the lab, used as a probe endpoint."""
    names = list(getattr(net_env, "hosts", None) or []) + sorted(
        getattr(net_env, "machine_identities", None) or {}
    )
    return next(
        (n for n in names if n not in exclude and _single_homed(net_env, n)), None
    )


def _probe_ends(net_env, node: str, intf: str) -> tuple[str, tuple[str, str]] | None:
    """Probe source and addressed target whose path crosses ``node:intf`` outbound.

    Both link ends when both hold addresses. When one end is a switch (OVS,
    BMv2, SR Linux), the addressed end must be an end host and the other probe
    end is a host elsewhere in the lab, reached through that switch. ``None``
    when no such pair exists (for example a switch-to-switch link).
    """
    peer = _link_peer(net_env, node, intf)
    if peer is None:
        return None
    if is_ip_node(net_env, node) and is_ip_node(net_env, peer[0]):
        return node, peer
    if _single_homed(net_env, node):
        remote = _remote_host(net_env, {node, peer[0]})
        if remote is not None:
            return node, (remote, resolve_default_intf("eth0", net_env))
    if _single_homed(net_env, peer[0]):
        remote = _remote_host(net_env, {node, peer[0]})
        if remote is not None:
            return remote, peer
    return None


def _burst_loss_ping(dst: str) -> str:
    """60 probes at 200 ms; prints received sequence numbers and the summary."""
    return (
        f"ping -i 0.2 -c 60 -W 1 {dst} 2>&1 | awk '/bytes from/"
        '{for(i=1;i<=NF;i++) if($i ~ /^icmp_seq=/) printf "%s ", $i} '
        '/packets transmitted/{print ""; print}\''
    )


def _loss_runs(output: str) -> list[int]:
    """Lengths of runs of consecutive unanswered probes in ``_burst_loss_ping``."""
    sent = re.search(r"(\d+) packets transmitted", output)
    if sent is None:
        return []
    got = {int(seq) for seq in re.findall(r"icmp_seq=(\d+)", output)}
    if not got:
        return []
    runs, run = [], 0
    for seq in range(1, int(sent.group(1)) + 1):
        if seq in got:
            if run:
                runs.append(run)
            run = 0
        else:
            run += 1
    if run:
        runs.append(run)
    return runs


_ON_OFF_LOSS = Check(
    "probes are lost in at least two separate bursts of 3+ consecutive probes "
    "while the probes between bursts get replies (on/off outages)",
    lambda out: sum(run >= 3 for run in _loss_runs(out)) >= 2,
)


def _receiver_bps(output: str) -> float | None:
    match = _RATE_RE.search(output)
    if match is None:
        return None
    return float(match.group(1)) * _RATE_SCALE[match.group(2)]


def _saturated_link(output: str) -> bool:
    """iperf3 receiver rate is low while concurrent pings see queueing delay."""
    bps = _receiver_bps(output)
    rtt = re.search(r"rtt [^=]+= [0-9.]+/([0-9.]+)/", output)
    return _below_2mbps(bps) and rtt is not None and float(rtt.group(1)) > 50


def _below_2mbps(rate: float | None) -> bool:
    """A measured rate (0 included) under 2 Mbit/s; no measurement is not."""
    return rate is not None and rate < 2e6


# ==================================================================
# Problem: Link failure through controller-owned link endpoints
# ==================================================================


class LinkFailureParams(BaseModel):
    """Parameters for injecting a link-down fault."""

    host_name: str = Field(description="Target host name.")
    intf_name: str = Field(default="eth0", description="Target interface name.")


class LinkFailure(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "link_down"
    description = "Carrier or operational link is down on the selected attachment."
    TAGS: str = ["link"]
    supported_backends = ("kathara", "containerlab")
    # Sets the container interface down; native routers mirror its link state.
    native_router_operations = frozenset()
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        self.faulty_intf = intf
        self._set_link_operational_down(params.host_name, intf)
        self._link_down_peer = self._peer_endpoint(params.host_name, intf)
        if self._link_down_peer is not None:
            peer_host, peer_intf = self._link_down_peer
            self._set_link_operational_down(peer_host, peer_intf)

    def _inject_link_down_containerlab(self, params: LinkFailureParams) -> None:
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        controller = HostTcController(self.runtime)
        operstate = self.runtime.get_interface_operstate(params.host_name, intf)
        target = getattr(self, "_link_down_target", None)
        peer = target or controller.peer_name(params.host_name, intf)
        artifact_ok = controller.link_peer_down(peer)
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

    def oracle_playbook(self, params: LinkFailureParams) -> OraclePlaybook:
        intf = resolve_default_intf(params.intf_name, self.net_env)
        host = params.host_name
        steps = [
            shell(
                host,
                "ip -br link; ip route",
                f"List interface states and routes on {host}, an endpoint of the unreachable paths.",
            ),
            iface_down(host, intf),
        ]
        peer = self._peer_endpoint(host, intf)
        if peer is not None:
            steps.append(iface_down(*peer, evidence=False))
        return OraclePlaybook(
            finding=f"The link on {host}:{intf} has no carrier, so every path across it is cut.",
            steps=steps,
        )

    def recover_fault(self, params: LinkFailureParams) -> dict:
        """Restore carrier on the selected attachment."""
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
            target = getattr(self, "_link_down_target", None)
            peer = target or controller.peer_name(params.host_name, intf)
            controller.set_link_up(peer)
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
    symptom_probe = "custom"
    symptom_class = "loss"
    dynamic_artifact = True
    root_cause_name: str = "link_flap"
    description = "Logical link flaps between up and down."
    TAGS: str = ["link"]
    # Acts on the link through a controller-side proxy, outside the router OS.
    native_router_operations = frozenset()
    BENCHMARK_LINK_PROXY = True
    supported_backends = ("kathara", "containerlab")
    RECORDED_ATTRS = ("faulty_intf", "_controller_target")

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

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        # Only the SR Linux client-facing port recovers between flap cycles.
        if ctx.scenario == "min3clos":
            return [base]
        return super().benchmark_inject_options(ctx, base)

    symptom_desc = "Users report connectivity issues to other hosts."

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.faulty_intf = "eth0"
        self._controller_target = None

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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        self._inject_link_flap(params, intf, backend="kathara")

    def _inject_link_flap_containerlab(self, params: LinkFlapParams) -> None:
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
            peer = controller.peer_name(params.host_name, intf_name)
            controller.start_link_flap(peer, params.down_time, params.up_time)
            self._controller_target = peer
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        return self._verify_link_flap(params, intf)

    def _verify_link_flap_containerlab(self, params: LinkFlapParams) -> dict:
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
            target = self._containerlab_flap_target(params.host_name, intf_name)
            running = controller.link_flap_running(target)
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=running,
            details={
                "host": params.host_name,
                "intf": intf_name,
            },
        )

    def oracle_playbook(self, params: LinkFlapParams) -> OraclePlaybook:
        intf = resolve_default_intf(params.intf_name, self.net_env)
        host = params.host_name
        steps = [
            shell(
                host,
                f"ip -br link show dev {intf}; ip -s link show dev {intf} | head -8",
                f"Inspect {host}:{intf}, the attachment on the lossy paths.",
            )
        ]
        if self.lab_backend == "containerlab":
            steps.append(
                shell(
                    host,
                    f"for i in $(seq 1 16); do cat /sys/class/net/{intf}/operstate; "
                    "sleep 0.5; done | sort | uniq -c",
                    f"Sample the operational state of {host}:{intf} for 8 seconds.",
                    all_of(
                        contains(r"\bup$", f"{host}:{intf} is seen up"),
                        contains(r"\bdown$", f"{host}:{intf} is seen down"),
                    ),
                    evidence=True,
                    timeout=20,
                )
            )
        else:
            steps.append(
                iface_state(
                    host,
                    intf,
                    contains(r"<[^>]*LOWER_UP", f"{host}:{intf} has carrier"),
                    f"Confirm {host}:{intf} itself is up, so the outages are not a "
                    "persistently down port.",
                )
            )
        src = params.symptom_host or params.observer_device
        purpose = (
            "Probe across the link every 200 ms for 12 seconds and look at which "
            "probes are lost."
        )
        if src and params.probe_dst_ip:
            steps.append(
                shell(
                    src,
                    _burst_loss_ping(params.probe_dst_ip),
                    purpose,
                    _ON_OFF_LOSS,
                    evidence=True,
                    timeout=40,
                )
            )
        elif (ends := _probe_ends(self.net_env, host, intf)) is not None:
            src, target = ends
            steps += [
                addr_step(*target, "flap_peer"),
                toward(
                    src,
                    "flap_peer",
                    _burst_loss_ping("{ip}"),
                    purpose,
                    _ON_OFF_LOSS,
                    evidence=True,
                    timeout=40,
                ),
            ]
        elif self.lab_backend != "containerlab":
            raise ValueError(
                f"link_flap on {host}:{intf}: no addressed probe path crosses "
                "the link, so the outages are not observable"
            )
        return OraclePlaybook(
            finding=(
                f"The link on {host}:{intf} repeatedly goes down for about a "
                "second and comes back, so traffic across it is lost in bursts."
            ),
            steps=steps,
        )

    def recover_fault(self, params: LinkFlapParams) -> dict:
        """Stop controller-side flapping and restore the logical link."""
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
            target = self._containerlab_flap_target(params.host_name, intf)
            controller.stop_link_flap(target)
            restored = not controller.link_flap_running(target)
        return {
            "verified": restored,
            "details": {"host": params.host_name, "intf": intf},
        }

    def _containerlab_flap_target(self, host: str, intf: str) -> str:
        recorded = getattr(self, "_controller_target", None)
        if recorded:
            return recorded
        return HostTcController(self.runtime).peer_name(host, intf)


# ==========================================
# Problem: Link capacity bottleneck (controller-side TBF)
# ==========================================


def _link_peer(net_env, node: str, intf: str) -> tuple[str, str] | None:
    """Other end of the point-to-point link at ``node:intf``."""
    needle = (node, intf)
    for _key, raw in iter_link_termination_points(net_env):
        ends = [parse_endpoint(str(item)) for item in raw]
        if len(ends) == 2 and needle in ends:
            return ends[1] if ends[0] == needle else ends[0]
    return None


def _attached_hosts(net_env, node: str) -> list[str]:
    """End hosts whose one data link ends on ``node``."""
    hosts = []
    for _key, raw in iter_link_termination_points(net_env):
        ends = [parse_endpoint(str(item))[0] for item in raw]
        if len(ends) == 2 and node in ends:
            other = ends[1] if ends[0] == node else ends[0]
            if _single_homed(net_env, other):
                hosts.append(other)
    return sorted(hosts)


def _hosts_across(net_env, node: str, intf: str) -> tuple[str, str] | None:
    """End hosts whose traffic leaves ``node`` through ``intf``.

    Each side is the link end itself when it is an end host, else a host
    attached to it. A side with neither uses a host elsewhere in the
    environment, as for a spine.
    """
    peer = _link_peer(net_env, node, intf)
    if peer is None:
        return None

    def side(end: str, other: str) -> list[str]:
        if _single_homed(net_env, end):
            return [end]
        return [h for h in _attached_hosts(net_env, end) if h != other]

    near, far = side(node, peer[0]), side(peer[0], node)
    elsewhere = _remote_host(net_env, {node, peer[0], *near, *far})
    near = near or ([elsewhere] if elsewhere else [])
    far = far or ([elsewhere] if elsewhere else [])
    return (near[0], far[0]) if near and far and near[0] != far[0] else None


def _iperf_step(src: str, dst: str, where: str) -> OracleStep:
    """TCP throughput from ``src`` to ``dst`` must stay under 2 Mbit/s."""
    return OracleStep(
        f"Measure TCP throughput from {src} to {dst} {where}.",
        "iperf_test",
        {
            "client_host_name": src,
            "server_host_name": dst,
            "duration": 10,
            "client_args": "-f k",
        },
        Check(
            "the path delivers under 2 Mbit/s",
            lambda out: _below_2mbps(_receiver_bps(out)),
        ),
        evidence=True,
    )


def _link_row(ctx, row: dict, node: str, intf: str) -> dict[str, str] | None:
    """``row`` moved to ``node:intf``, with ISP observers for that direction."""
    from nika.net_env.isp.identity import is_isp_scenario

    moved = {**row, "host_name": node, "intf_name": intf}
    if is_isp_scenario(ctx.scenario):
        from nika.net_env.isp.inject_targets import isp_link_symptom_targets

        try:
            moved.update(
                isp_link_symptom_targets(loaded_inventory(ctx.net_env), node, intf)
            )
        except ValueError:
            return None
    return moved


def _loaded_link_target(ctx, workload) -> dict[str, str] | None:
    """First point-to-point link egress the workload loads with bulk traffic."""
    for node, intf in sorted(workload.targets(BULK_TRANSIT)):
        if _link_peer(ctx.net_env, node, intf) is None:
            continue
        row = _link_row(ctx, {}, node, intf)
        if row is not None:
            return row
    return None


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
    symptom_probe = "custom"
    symptom_class = "degradation"
    dynamic_artifact = True
    root_cause_name: str = "link_capacity_bottleneck"
    description = "Logical link capacity is bottlenecked below demand."
    TAGS: str = ["link"]
    # Acts on the link through a controller-side proxy, outside the router OS.
    native_router_operations = frozenset()
    BENCHMARK_LINK_PROXY = True
    supported_backends = ("kathara", "containerlab")
    RECORDED_ATTRS = ("faulty_intf",)

    Params = LinkCapacityBottleneckParams

    BENCHMARK_TARGETS = "link"
    BENCHMARK_POINT_TO_POINT = True
    # A circuit below demand only shows under load: the shaped direction must
    # carry the scenario's sustained bulk traffic.
    SYMPTOM_WORKLOAD = BULK_TRANSIT

    @classmethod
    def symptom_workload_target(cls, inject):
        return (inject.get("host_name") or "", inject.get("intf_name") or "")

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        workload = ctx.net_env.business_workload()
        if workload is not None:
            # A carrier circuit provisioned at a 512 kbit/s tier under a
            # workload that needs at least three times that; control traffic
            # (IGP hellos, BGP keepalives) still fits.
            params = _loaded_link_target(ctx, workload)
            if params is not None:
                params.update(rate="512kbit", burst="16kb", limit="32kb")
                return params
            # No loaded link: workload validation rejects every target here.
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

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        # The shaper acts on traffic leaving ``host_name:intf_name``. Under a
        # business workload that loads one direction of a link, each end is a
        # distinct target.
        rows = super().benchmark_inject_options(ctx, base)
        if ctx.net_env.business_workload() is None:
            return rows
        reverse = []
        for row in rows:
            peer = _link_peer(ctx.net_env, row["host_name"], row["intf_name"])
            if peer is not None:
                reverse.append(_link_row(ctx, row, *peer))
        return unique(rows + [row for row in reverse if row is not None])

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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        self.faulty_intf = intf
        controller = KatharaVdeFaultProxy(self.runtime)
        self._proxy = controller.insert(params.host_name, intf)
        controller.set_tbf(
            self._proxy, rate=params.rate, burst=params.burst, limit=params.limit
        )

    def _inject_capacity_containerlab(
        self, params: LinkCapacityBottleneckParams
    ) -> None:
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        self.faulty_intf = intf
        controller = HostTcController(self.runtime)
        peer = controller.set_tbf(
            params.host_name,
            intf,
            rate=params.rate,
            burst=params.burst,
            limit=params.limit,
        )
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        controller = HostTcController(self.runtime)
        peer = getattr(self, "_host_veth", None) or controller.egress_dev(
            params.host_name, intf
        )
        verified = "tbf" in controller.qdisc(peer).lower()
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={"host": params.host_name, "intf": intf, "mode": "host_peer"},
        )

    def oracle_playbook(self, params: LinkCapacityBottleneckParams) -> OraclePlaybook:
        intf = resolve_default_intf(params.intf_name, self.net_env)
        host = params.host_name
        peer = _link_peer(self.net_env, host, intf)
        steps = [
            shell(
                host,
                f"ip -br link show dev {intf}; ip -s link show dev {intf} | head -8",
                f"Inspect {host}:{intf}, the attachment of the slow transfers.",
                contains(r"<[^>]*LOWER_UP", f"{host}:{intf} has carrier"),
            )
        ]
        direction = f"leaving {host}:{intf}"
        ends = _probe_ends(self.net_env, host, intf)
        across = _hosts_across(self.net_env, host, intf)
        if (
            self.lab_backend == "containerlab"
            and peer is not None
            and _role(self.net_env, peer[0])
        ):
            # Node-to-node links are shaped on their host-side wire, outside
            # both nodes, so only throughput across the link shows the cap.
            if params.probe_dst_ip and params.peer_host:
                src = params.symptom_host or params.observer_device or host
                dst = params.peer_host
            elif across is not None:
                src, dst = across
            else:
                raise ValueError(
                    f"link_capacity_bottleneck on {host}:{intf}: no host pair "
                    "measures the shaped direction"
                )
            steps.append(_iperf_step(src, dst, "across the link"))
        elif self.lab_backend == "containerlab":
            # A link to the host namespace is shaped on its host-side veth,
            # which transmits toward the node.
            direction = f"arriving at {host}:{intf}"
            remote = _remote_host(self.net_env, {host})
            if remote is None or not _single_homed(self.net_env, host):
                raise ValueError(
                    f"link_capacity_bottleneck on {host}:{intf}: no host pair "
                    "measures the shaped direction"
                )
            steps.append(
                OracleStep(
                    f"Measure TCP throughput from {remote} into {host}.",
                    "iperf_test",
                    {
                        "client_host_name": remote,
                        "server_host_name": host,
                        "duration": 10,
                        "client_args": "-f k",
                    },
                    Check(
                        "the transfer into the node runs under 2 Mbit/s",
                        lambda out: _below_2mbps(_receiver_bps(out)),
                    ),
                    evidence=True,
                )
            )
        elif ends is not None:
            src, (dst, dst_intf) = ends
            steps += [
                addr_step(dst, dst_intf, "cap_peer"),
                shell(
                    dst,
                    "nohup timeout 40 iperf3 -s -1 -p 5299 >/dev/null 2>&1 & "
                    "sleep 1; echo started",
                    f"Start a one-off iperf3 server on {dst}.",
                ),
                toward(
                    src,
                    "cap_peer",
                    "(sleep 3; ping -c 8 -i 0.5 -W 3 {ip} | tail -2) & "
                    "iperf3 -c {ip} -p 5299 -t 10 -f k 2>&1 "
                    "| grep -E 'sender|receiver|error'; wait",
                    f"Push TCP from {src} across the link to {dst} while pinging "
                    f"{dst}.",
                    Check(
                        "the link delivers under 2 Mbit/s while probes behind the "
                        "transfer see over 50 ms of queueing delay",
                        _saturated_link,
                    ),
                    evidence=True,
                    timeout=45,
                ),
            ]
        elif params.probe_dst_ip and params.peer_host:
            src = params.symptom_host or params.observer_device or host
            steps.append(_iperf_step(src, params.peer_host, "across the link"))
        else:
            raise ValueError(
                f"link_capacity_bottleneck on {host}:{intf}: no addressed probe "
                "path crosses the link in the shaped direction"
            )
        return OraclePlaybook(
            finding=(
                f"The link capacity for traffic {direction} is provisioned far "
                "below the traffic it carries, so transfers in that direction "
                "are capped and queue behind it."
            ),
            steps=steps,
        )

    def recover_fault(self, params: LinkCapacityBottleneckParams) -> dict:
        """Remove controller-side capacity limiting and restore the logical link."""
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
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
            peer = getattr(self, "_host_veth", None) or controller.egress_dev(
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
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "link_detach"
    description = "Network attachment is detached; the interface is gone from the node."
    TAGS: str = ["link"]
    supported_backends = ("kathara", "containerlab")
    # Moves the container interface out of the node, outside the router OS;
    # recovery asks the router to bind its port to the returned interface.
    native_router_operations = frozenset({"interface_up", "reattach_interface"})
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
        self._detach_holder: str | None = None
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
        # Both families: a dual-stack host also loses its IPv6 default route.
        self._saved_routes = []
        for family in ("-4", "-6"):
            route_out = self.runtime.exec(host, f"ip -j {family} route show dev {intf}")
            rows = parse_ip_route_json(route_out)
            if rows is None:
                raise RuntimeError(
                    f"could not parse routes on {host}:{intf}: {route_out}"
                )
            for row in rows:
                cmd = static_route_replace_command(family, row, intf)
                if cmd:
                    self._saved_routes.append(cmd)

    def _restore_l3_state(self, host: str, intf: str) -> None:
        for cidr in self._saved_addrs:
            self.runtime.exec(
                host, f"ip addr replace {cidr} dev {intf} 2>/dev/null || true"
            )
        for cmd in self._saved_routes:
            out = self.runtime.exec(host, f"{cmd} && echo {_ROUTE_RESTORE_OK}")
            if _ROUTE_RESTORE_OK not in out:
                raise RuntimeError(f"restoring route failed: {cmd}: {out}")
        self._saved_addrs = []
        self._saved_routes = []

    def inject_fault(self, params: LinkDetachParams):
        """Move the attachment into a netns held outside the node.

        A holder process inside the node (``unshare -n sleep infinity``) would
        sit in its process list next to the missing interface. The holder is a
        controller-side container labelled for the lab, so lab teardown removes
        it even when recovery never runs. A router with a fault-operations
        provider sees the loss as its own port going down.
        """
        if self.lab_backend not in ("kathara", "containerlab"):
            raise RuntimeCapabilityError(
                f"{type(self).__name__} cannot inject_fault: unsupported backend "
                f"{self.lab_backend!r}."
            )
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        self.faulty_intf = intf
        host = params.host_name
        native_fault_ops(self, host, "reattach_interface")
        self._save_l3_state(host, intf)
        node = self.runtime.get_container(host)
        holder = node.client.containers.run(
            nika_image("base"),
            command=["sleep", "infinity"],
            network_mode="none",
            detach=True,
            labels={"nika.lab_name": self.runtime.lab_name},
        )
        self._detach_holder = holder.id
        holder.reload()
        kept = False
        try:
            self._move_netns(node, f"container:{node.id}", intf, holder)
            deadline = time.time() + 5.0
            while self.runtime.interface_exists(host, intf):
                if time.time() > deadline:
                    raise RuntimeError(
                        f"link_detach failed: {host}:{intf} still present"
                    )
                time.sleep(0.2)
            kept = True
        finally:
            if not kept:
                try:
                    holder.remove(force=True)
                except Exception:
                    pass
                self._detach_holder = None
        system_logger.info(f"Injected link detach on {host}:{intf} (host-side netns)")

    def _move_netns(self, node, network_mode: str, intf: str, target) -> None:
        """Move ``intf`` from the netns of ``network_mode`` to ``target``'s."""
        target.reload()
        pid = int(target.attrs["State"]["Pid"])
        output = run_labelled_helper(
            node.client,
            nika_image("base"),
            ["ip", "link", "set", "dev", intf, "netns", str(pid)],
            lab_name=self.runtime.lab_name,
            network_mode=network_mode,
            pid_mode="host",
            privileged=True,
        )
        text = output.strip() if isinstance(output, bytes) else str(output).strip()
        if text:
            raise RuntimeError(f"moving {intf} to pid {pid}: {output!r}")

    def verify_fault(self, params: LinkDetachParams) -> dict:
        """Verify the interface is gone from the node namespace."""
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        interface_gone = not self.runtime.interface_exists(params.host_name, intf)
        details = {
            "host": params.host_name,
            "intf": intf,
            "interface_exists": not interface_gone,
        }
        nos_down = True
        ops = native_fault_ops(self, params.host_name, "interface_up")
        if ops is not None and ops.port_follows_link:
            nos_down = not ops.interface_up(intf)
            details["router_port_up"] = not nos_down
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=interface_gone and nos_down,
            details=details,
        )

    def oracle_playbook(self, params: LinkDetachParams) -> OraclePlaybook:
        intf = resolve_default_intf(params.intf_name, self.net_env)
        host = params.host_name
        steps = [
            shell(
                host,
                "ip -br link; ip -4 route",
                f"List interfaces and routes on {host}, an endpoint of the "
                "unreachable paths.",
            ),
            shell(
                host,
                f"ip link show dev {intf} 2>&1; ls /sys/class/net",
                f"Look up {host}:{intf} in the node's interface list.",
                all_of(
                    contains(r"does not exist", f"{intf} is unknown to {host}"),
                    lacks(rf"^{re.escape(intf)}$", f"{intf} is absent from sysfs"),
                ),
                evidence=True,
            ),
        ]
        peer = _link_peer(self.net_env, host, intf)
        if peer is not None and is_ip_node(self.net_env, peer[0]):
            steps.append(
                iface_state(
                    *peer,
                    contains(
                        r"<[^>]*LOWER_UP", f"{peer[0]}:{peer[1]} still has carrier"
                    ),
                    f"Check the far end {peer[0]}:{peer[1]} of the attachment.",
                    evidence=False,
                )
            )
        return OraclePlaybook(
            finding=(
                f"{host}:{intf} no longer exists on {host}: the attachment is "
                "detached, so the node has no path over that link."
            ),
            steps=steps,
        )

    def recover_fault(self, params: LinkDetachParams) -> dict:
        """Move the detached interface back into the node namespace."""
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        host = params.host_name
        if self._detach_holder:
            node = self.runtime.get_container(host)
            holder = node.client.containers.get(self._detach_holder)
            try:
                self._move_netns(node, f"container:{holder.id}", intf, node)
            finally:
                holder.remove(force=True)
                self._detach_holder = None
        self.runtime.exec(host, f"ip link set dev {intf} up 2>/dev/null || true")
        self._restore_l3_state(host, intf)
        details = {"host": host, "intf": intf}
        restored = self.runtime.interface_exists(host, intf)
        ops = native_fault_ops(self, host, "reattach_interface")
        if ops is not None and restored:
            ops.reattach_interface(intf)
            restored = wait_until(lambda: ops.interface_up(intf), 60)
            details["router_port_up"] = restored
        details["interface_exists"] = self.runtime.interface_exists(host, intf)
        return {"verified": restored, "details": details}
