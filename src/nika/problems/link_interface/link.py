from nika.net_env.utils.endpoint_network import restore_endpoint_network
import re
import shlex
import time

from pydantic import BaseModel, Field

from nika.problems.base import (
    FailureDomain,
    ProblemBase,
    build_verify_result,
)
from nika.problems.support.oracle import (
    interface_read,
    interface_state,
    addr_step,
    is_ip_node,
    toward,
    Check,
    OraclePlaybook,
    OracleStep,
    shell,
)
from nika.problems.rca.inventory import (
    kernel_data_interface,
    iter_link_termination_points,
    link_containing_endpoint,
    parse_endpoint,
    resolve_default_intf,
)
from nika.problems.support.router_ops import native_router_profile
from nika.problems.support.benchmark_targets import (
    CARRIER_FLAP_UP_SEC,
    endpoint_target,
    isp_link_target,
    loaded_inventory,
    resolve_link_flap_params,
    unique,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from nika.service.containerlab.host_tc import HostTcController
from nika.traffic.workload import BULK_TRANSIT, FAN_IN_BURSTS
from nika.utils.logger import system_logger


# ------------------------------------------------------------------
# Oracle helpers
# ------------------------------------------------------------------

_RATE_RE = re.compile(r"([0-9.]+)\s*([KMG]?)bits/sec\b.*\breceiver\b")
_RATE_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "G": 1e9}


def _role(net_env, node: str) -> str | None:
    identity = (getattr(net_env, "machine_identities", None) or {}).get(node)
    return None if identity is None else getattr(identity.role, "value", identity.role)


def _interface_states(output: str) -> set[str]:
    """Operational carrier states from the platform's selected-interface read."""
    states = set()
    for state, pattern in {
        "up": r"state UP|operstate[\" :]+UP|oper-state up|link_state\s*:\s*up|is up|^true\s*$|^Ethernet\d+.*\bup\s+up\b",
        "down": r"state (?:DOWN|LOWERLAYERDOWN)|operstate[\" :]+DOWN|oper-state down|link_state\s*:\s*down|is (?:administratively )?down|^false\s*$|^Ethernet\d+.*\bdown\b",
    }.items():
        if re.search(pattern, output, re.I | re.M):
            states.add(state)
    return states


def _single_homed(net_env, node: str) -> bool:
    """An end host: every path to or from it crosses its one data link."""
    return is_ip_node(net_env, node) and _role(net_env, node) in (
        "host",
        "server",
        "service",
    )


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
    when no such pair exists (for example a switch-to-switch link). The
    target interface carries the kernel name its node reports.
    """
    peer = _link_peer(net_env, node, intf)
    if peer is None:
        return None
    src, target = None, None
    if is_ip_node(net_env, node) and is_ip_node(net_env, peer[0]):
        src, target = node, peer
    elif _single_homed(net_env, node):
        remote = _remote_host(net_env, {node, peer[0]})
        if remote is not None:
            src, target = node, (remote, resolve_default_intf("eth0", net_env, remote))
    if target is None and _single_homed(net_env, peer[0]):
        remote = _remote_host(net_env, {node, peer[0]})
        if remote is not None:
            src, target = remote, peer
    if target is None:
        return None
    return src, (target[0], kernel_data_interface(net_env, *target))


def _burst_loss_ping(
    dst: str, seconds: int = 12, carrier_intf: str | None = None
) -> str:
    """Sample timely replies with a fresh socket so delayed echoes count as lost."""
    carrier = ""
    if carrier_intf is not None:
        path = shlex.quote(f"/sys/class/net/{carrier_intf}/carrier")
        carrier = f"carrier=$(cat {path} 2>/dev/null) || carrier=?; printf 'carrier=%s ' \"$carrier\"; "
    return (
        f"end=$(($(date +%s)+{seconds})); sent=0; received=0; "
        'while [ "$(date +%s)" -lt "$end" ]; do '
        f"{carrier}"
        "sent=$((sent+1)); "
        f"if ping -n -c 1 -W 0.1 {shlex.quote(dst)} >/dev/null 2>&1; then "
        'received=$((received+1)); printf "icmp_seq=%s " "$sent"; fi; '
        "sleep 0.1; done; "
        'printf "\n%s packets transmitted, %s received\n" "$sent" "$received"'
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


def _flap_loss_or_carrier(output: str) -> bool:
    """Accept sustained loss only with repeated, complete carrier observations."""
    if _ON_OFF_LOSS(output):
        return True
    summary = re.search(r"(\d+) packets transmitted, (\d+) received", output)
    if summary is None:
        return False
    sent, received = map(int, summary.groups())
    carrier = re.findall(r"carrier=([01?])", output)
    return bool(
        sent >= 10
        and received <= sent
        and (sent - received) / sent > 0.8
        and len(carrier) == sent
        and "?" not in carrier
        and len(re.findall(r"0{3,}", "".join(carrier))) >= 3
        and len(re.findall(r"1{3,}", "".join(carrier))) >= 3
    )


_FLAP_LOSS_OR_CARRIER = Check(
    "loss in repeated bursts, or sustained loss with repeated carrier cycles",
    _flap_loss_or_carrier,
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
    NATIVE_LOG_KEYWORDS = ("mac_(local|remote)_fault",)
    CONFUSABLE_WITH = ("link_flap",)
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
                # BMv2 keeps packet sockets bound to its interfaces. Disable
                # the endpoint attachment without disrupting the switch process.
                if peer_host in (self.net_env.bmv2_switches or []):
                    return None
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        host = params.host_name
        started = None
        observations = []

        def stays_down(output):
            nonlocal started
            now = time.monotonic()
            if started is None:
                started = now
            observations.append(_interface_states(output))
            return now - started >= 12 and all(
                states == {"down"} for states in observations
            )

        down = Check(
            "the attachment stays down throughout a 12-second observation", stays_down
        )
        steps = [
            interface_state(
                self.net_env,
                host,
                intf,
                f"Sample {host}:{intf} to distinguish a sustained outage from carrier flapping.",
                down,
                evidence=True,
                attempts=25,
                interval_sec=0.5,
            )
        ]
        peer = self._peer_endpoint(host, intf)
        if peer is not None:
            steps.append(
                interface_state(
                    self.net_env, *peer, "Read the far-end interface state."
                )
            )

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
            restore_endpoint_network(self.runtime, self.net_env, params.host_name)
            if peer is not None:
                restore_endpoint_network(self.runtime, self.net_env, peer[0])
            restore_routes = getattr(self.net_env, "restore_link_routes", None)
            if restore_routes is not None:
                restore_routes(self.runtime, params.host_name, intf)
                if peer is not None:
                    restore_routes(self.runtime, *peer)
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


_ISP_PROBE_KEYS = ("symptom_host", "probe_dst_ip", "peer_host")


class LinkFlap(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    symptom_probe = "custom"
    symptom_class = "loss"
    dynamic_artifact = True
    root_cause_name: str = "link_flap"
    NATIVE_LOG_KEYWORDS = ("mac_(local|remote)_fault",)
    CONFUSABLE_WITH = ("link_down", "silent_egress_packet_loss")
    description = "Logical link flaps between up and down."
    TAGS: str = ["link"]
    # Takes the link's carrier down from outside the router OS, as a failing
    # cable or transceiver would.
    native_router_operations = frozenset()
    BENCHMARK_LINK_PROXY = True
    supported_backends = ("kathara", "containerlab")
    RECORDED_ATTRS = ("faulty_intf", "_controller_target")

    Params = LinkFlapParams

    BENCHMARK_TARGETS = "link"
    BENCHMARK_POINT_TO_POINT = True

    @staticmethod
    def _isp_access_link(ctx, row: dict[str, str]) -> dict[str, str]:
        """Move a backbone link row to the access link of its probe source.

        Routers reroute around a flapping backbone link as soon as its carrier
        drops, so an ISP flap hits the single-homed access link instead, as a
        failing customer port or optic would.
        """
        from nika.net_env.isp.inject_targets import edge_link_iface

        intf = edge_link_iface(loaded_inventory(ctx.net_env), row["host_name"])
        return {**row, "intf_name": intf}

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            params = cls._isp_access_link(ctx, isp_link_target(ctx))
            params["down_time"] = "1"
            params["up_time"] = CARRIER_FLAP_UP_SEC
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
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            from nika.net_env.isp.inject_targets import isp_link_symptom_targets

            # One access link per router, probed toward a backbone peer's host.
            inventory = loaded_inventory(ctx.net_env)
            aux = {
                key: value
                for key, value in base.items()
                if key not in {"host_name", "intf_name", *_ISP_PROBE_KEYS}
            }
            bgp = inventory.get("bgp") or {}
            asn_of = (
                {node["device"]: node["asn"] for node in bgp.get("nodes") or []}
                if bgp.get("bgp_mode") == "ebgp"
                else {}
            )
            rows: dict[str, dict[str, str]] = {}
            for link in inventory.get("links") or []:
                ends = (link["endpoint_a"]["device"], link["endpoint_b"]["device"])
                # Edge stubs of different ASes do not reach each other.
                if asn_of and asn_of.get(ends[0]) != asn_of.get(ends[1]):
                    continue
                for side in ("endpoint_a", "endpoint_b"):
                    device = str(link[side]["device"])
                    if device in rows:
                        continue
                    row = {
                        **aux,
                        "host_name": device,
                        "intf_name": str(link[side]["iface"]),
                        **isp_link_symptom_targets(
                            inventory, device, str(link[side]["iface"])
                        ),
                    }
                    rows[device] = cls._isp_access_link(ctx, row)
            return [rows[device] for device in sorted(rows)]
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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        host = params.host_name
        period = params.down_time + params.up_time
        probe_seconds = max(12, 3 * period)
        states = set()

        def changes(output):
            states.update(_interface_states(output))
            return states == {"up", "down"}

        state_step = interface_state(
            self.net_env,
            host,
            intf,
            f"Sample {host}:{intf} until both carrier states are observed.",
            Check("the interface transitions between up and down", changes),
            evidence=True,
            attempts=max(16, 4 * period),
            interval_sec=0.5,
        )
        steps = [state_step]
        src = params.symptom_host or params.observer_device
        purpose = (
            f"Probe across the link with a 100 ms reply deadline for {probe_seconds} seconds and look at which "
            "probes are lost."
        )
        if src and params.probe_dst_ip:
            identity = self.net_env.machine_identities.get(host)
            carrier_intf = (
                kernel_data_interface(self.net_env, host, intf)
                if src == host and identity and "linux" in identity.capabilities
                else None
            )
            steps.append(
                shell(
                    src,
                    _burst_loss_ping(
                        params.probe_dst_ip,
                        seconds=probe_seconds,
                        carrier_intf=carrier_intf,
                    ),
                    purpose,
                    _FLAP_LOSS_OR_CARRIER if carrier_intf else _ON_OFF_LOSS,
                    evidence=True,
                    timeout=probe_seconds + 10,
                )
            )
        elif (ends := _probe_ends(self.net_env, host, intf)) is not None:
            src, target = ends
            router_probe = _role(self.net_env, src) == "router"
            probe_template = _burst_loss_ping("{ip}", seconds=probe_seconds)
            probe_check = _ON_OFF_LOSS
            probe_attempts = 1
            if router_probe:
                probe_template = "ping -n -c 60 -i 0.1 -W 0.1 {ip}"
                observations = {"sent": 0, "received": set()}

                def burst_samples(output):
                    sent = re.search(r"(\d+) packets transmitted", output)
                    if sent is None:
                        return False
                    offset = observations["sent"]
                    observations["received"].update(
                        offset + int(match[1])
                        for match in re.finditer(
                            r"(?m)^\d+ bytes from .*?icmp_seq=(\d+)", output
                        )
                    )
                    observations["sent"] += int(sent[1])
                    combined = (
                        " ".join(
                            f"icmp_seq={seq}"
                            for seq in sorted(observations["received"])
                        )
                        + f"\n{observations['sent']} packets transmitted"
                    )
                    return _ON_OFF_LOSS(combined)

                probe_check = Check(_ON_OFF_LOSS.description, burst_samples)
                probe_attempts = max(3, (probe_seconds + 5) // 6)
            steps += [
                addr_step(*target, "flap_peer"),
                toward(
                    src,
                    "flap_peer",
                    probe_template,
                    purpose,
                    probe_check,
                    evidence=True,
                    timeout=probe_seconds + 10,
                    attempts=probe_attempts,
                    interval_sec=0.2,
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
    attached to it. For a deeper hierarchy, choose nearby end hosts without
    traversing the other endpoint while searching that side.
    """
    peer = _link_peer(net_env, node, intf)
    if peer is None:
        return None

    def side(end: str, other: str) -> list[str]:
        if _single_homed(net_env, end):
            return [end]
        return [h for h in _attached_hosts(net_env, end) if h != other]

    near, far = side(node, peer[0]), side(peer[0], node)
    if near and far and near[0] != far[0]:
        return near[0], far[0]

    from collections import defaultdict, deque

    graph = defaultdict(set)
    for _key, endpoints in iter_link_termination_points(net_env):
        nodes = {parse_endpoint(str(endpoint))[0] for endpoint in endpoints}
        if len(nodes) != 2:
            # Keep shared segments out of the point-to-point probe graph.
            continue
        for end in nodes:
            graph[end].update(nodes - {end})

    def nearest(end, other):
        queue, seen, hosts = deque([(end, 0)]), {other, end}, []
        while queue:
            current, distance = queue.popleft()
            if _single_homed(net_env, current):
                hosts.append((distance, current))
                continue
            for neighbor in sorted(graph[current] - seen):
                seen.add(neighbor)
                queue.append((neighbor, distance + 1))
        return hosts

    left_hosts = {host: distance for distance, host in nearest(node, peer[0])}
    right_hosts = {host: distance for distance, host in nearest(peer[0], node)}
    pairs = [
        (left_distance + right_distance, left, right)
        for left, left_distance in left_hosts.items()
        for right, right_distance in right_hosts.items()
        if left_distance < right_hosts.get(left, float("inf"))
        and right_distance < left_hosts.get(right, float("inf"))
        if left != right
    ]
    if not pairs:
        return None
    _, left, right = min(pairs)
    return left, right


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
    CONFUSABLE_WITH = (
        "overlay_qos_ef_undersized",
        "p4_ecn_marking_disabled",
        "tcp_receive_window_limited",
        "link_latency",
    )
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
    # Admission below checks sustained bulk traffic or a declared fan-in receiver.
    SYMPTOM_WORKLOAD = None

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
            for (receiver,) in sorted(workload.targets(FAN_IN_BURSTS)):
                peer = _link_peer(
                    ctx.net_env,
                    receiver,
                    resolve_default_intf("eth0", ctx.net_env, receiver),
                )
                if peer is not None:
                    return {
                        "host_name": peer[0],
                        "intf_name": peer[1],
                        "rate": "128kbit",
                        "burst": "8kb",
                        "limit": "16kb",
                    }
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
        workload = ctx.net_env.business_workload()
        if workload is not None and not workload.targets(BULK_TRANSIT):
            return [base] if workload.targets(FAN_IN_BURSTS) else []
        rows = super().benchmark_inject_options(ctx, base)
        if workload is None:
            return rows
        reverse = []
        for row in rows:
            peer = _link_peer(ctx.net_env, row["host_name"], row["intf_name"])
            if peer is not None:
                reverse.append(_link_row(ctx, row, *peer))
        return unique(rows + [row for row in reverse if row is not None])

    @classmethod
    def validate_combined_inject(cls, ctx, inject):
        """Another failure can move traffic onto an otherwise idle circuit."""

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        workload = ctx.net_env.business_workload()
        if workload is None:
            return
        node, intf = inject.get("host_name"), inject.get("intf_name")
        if workload.covers(BULK_TRANSIT, (node, intf)):
            return
        peer = _link_peer(ctx.net_env, node, intf)
        if peer is not None and workload.covers(FAN_IN_BURSTS, (peer[0],)):
            return
        raise ValueError(
            "link_capacity_bottleneck needs bulk transit or the egress toward a declared fan-in receiver"
        )

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
        intf = resolve_default_intf(params.intf_name, self.net_env, params.host_name)
        host = params.host_name
        peer = _link_peer(self.net_env, host, intf)
        steps = [
            interface_read(
                self.net_env,
                host,
                intf,
                f"Inspect {host}:{intf}, the attachment of slow transfers.",
            )
        ]
        identity = self.net_env.machine_identities.get(host)
        if identity is not None and {"bmv2", "telemetry"} <= set(identity.capabilities):
            port = next(
                item.bmv2_port
                for item in self.net_env.model.ports[host]
                if item.name == intf
            )
            from nika.net_env.p4_dc_gateway.apply import QUEUE_DEPTH_PACKETS
            from nika.problems.support.p4_oracle import table_read

            steps.append(
                table_read(
                    host,
                    "ecn_config",
                    "the congested egress has a reachable ECN marking threshold",
                    lambda rows: any(
                        int(row["match"]["egress_port"]) == port
                        and 0 < int(row["params"]["threshold"]) < QUEUE_DEPTH_PACKETS
                        for row in rows
                    ),
                    "Check ECN marking configuration before attributing congestion to link capacity.",
                )
            )
            steps.append(
                OracleStep(
                    "Read the measured INT queue occupancy on this egress.",
                    "prometheus_query",
                    {
                        "query": f'max_over_time(p4_int_queue_depth_packets{{switch="{host}",egress_port="{port}"}}[2m])'
                    },
                )
            )
        direction = f"leaving {host}:{intf}"
        ends = _probe_ends(self.net_env, host, intf)
        across = _hosts_across(self.net_env, host, intf)
        if across is not None:
            # Native router containers expose Linux tools, but their shell
            # does not route through the NOS dataplane. Measure between hosts.
            src, dst = across
            ends = (src, (dst, kernel_data_interface(self.net_env, dst)))
        elif ends is not None and any(
            native_router_profile(self.net_env, node) for node in (ends[0], ends[1][0])
        ):
            ends = None
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
                    # Listen in the link interface's VRF when it has one, so
                    # the transfer arriving on that link can connect.
                    f"vrf=$(ip -o link show dev {dst_intf} "
                    "| sed -n 's/.* master \\([^ ]*\\) .*/\\1/p'); "
                    'run=""; ip vrf show "$vrf" >/dev/null 2>&1 '
                    '&& [ -n "$vrf" ] && run="ip vrf exec $vrf"; '
                    "nohup $run timeout 40 iperf3 -s -1 -p 5299 >/dev/null 2>&1 & "
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
