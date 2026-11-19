"""Propagation delay on a controller-owned link."""

import re

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.link_interface.link import _probe_ends, _remote_host, _receiver_bps
from nika.problems.rca.inventory import (
    link_containing_endpoint,
    select_host_interface,
    kernel_data_interface,
    resolve_default_intf,
)
from nika.problems.support.benchmark_targets import (
    isp_link_target,
    resolve_link_corruption_params,
)
from nika.problems.support.oracle import (
    OraclePlaybook,
    OracleStep,
    Check,
    addr_step,
    shell,
    toward,
)
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from nika.runtime.spec import NodeRole
from nika.service.containerlab.host_tc import HostTcController


def ping_average(output: str) -> float | None:
    match = re.search(r"(?:rtt|round-trip)[^=]+=\s*[\d.]+/([\d.]+)/", output)
    return float(match[1]) if match else None


def delay_configured(output: str, delay_ms: int) -> bool:
    match = re.search(r"\bdelay\s+([\d.]+)(us|ms|s)\b", output)
    return bool(
        match
        and "netem" in output
        and abs(
            float(match[1]) * {"us": 0.001, "ms": 1, "s": 1000}[match[2]] - delay_ms
        )
        < 0.01
    )


class LinkLatencyParams(BaseModel):
    host_name: str = Field(description="Node attached to the delayed link.")
    intf_name: str | None = Field(
        default=None, description="Link attachment interface."
    )
    delay_ms: int = Field(
        default=100,
        ge=10,
        le=1000,
        description="Added one-way link delay in milliseconds.",
    )
    probe_dst_ip: str | None = None
    observer_device: str | None = None
    symptom_host: str | None = None
    peer_host: str | None = None


class LinkLatency(ProblemBase):
    failure_domain = FailureDomain.LINK_INTERFACE
    root_cause_name = "link_latency"
    root_cause_owner = "link"
    description = "A link has excessive propagation delay."
    symptom_desc = "Business requests are slower on paths crossing one link."
    symptom_probe = "custom"
    symptom_class = "latency"
    CONFUSABLE_WITH = ("link_capacity_bottleneck", "link_packet_corruption")
    TAGS = ["link"]
    supported_backends = ("kathara", "containerlab")
    native_router_operations = frozenset()
    minimum_path_rate_kbps = 10_000
    BENCHMARK_LINK_PROXY = True
    BENCHMARK_TARGETS = "canonical"
    BENCHMARK_POINT_TO_POINT = True
    Params = LinkLatencyParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        from nika.net_env.isp.identity import is_isp_scenario

        if is_isp_scenario(ctx.scenario):
            return isp_link_target(ctx)
        target = resolve_link_corruption_params(
            ctx.scenario,
            ctx.net_env,
            ctx.rng,
            ctx.backend,
            host_pool=ctx.host_pool,
            host0=ctx.host0,
        )
        target.pop("corruption_percentage", None)
        target["intf_name"] = resolve_default_intf(
            target["intf_name"], ctx.net_env, target["host_name"]
        )
        return target

    def _intf(self, params):
        requested = params.intf_name or select_host_interface(
            self.net_env, params.host_name, last=True
        )
        return resolve_default_intf(requested, self.net_env, params.host_name)

    def root_cause_resources(self, params):
        return [
            link_containing_endpoint(self.net_env, params.host_name, self._intf(params))
        ]

    def inject_fault(self, params):
        intf = self._intf(params)
        if self.lab_backend == "containerlab":
            controller = HostTcController(self.runtime)
            kernel_intf = kernel_data_interface(self.net_env, params.host_name, intf)
            dev = controller.egress_dev(params.host_name, kernel_intf)
            if "netem" in controller.qdisc(dev) or "tbf" in controller.qdisc(dev):
                raise RuntimeError("Link already has a controller qdisc.")
            self._wire_dev = dev
            controller.set_delay(params.host_name, kernel_intf, params.delay_ms)
        else:
            controller = KatharaVdeFaultProxy(self.runtime)
            if controller.discover(params.host_name, intf) is not None:
                raise RuntimeError("Link already has a controller proxy.")
            self._proxy = controller.insert(params.host_name, intf)
            controller.set_netem_delay(self._proxy, params.delay_ms)

    def verify_fault(self, params):
        if self.lab_backend == "containerlab":
            dev = getattr(self, "_wire_dev", None)
            output = HostTcController(self.runtime).qdisc(dev) if dev else ""
        else:
            proxy = getattr(self, "_proxy", None)
            output = (
                KatharaVdeFaultProxy(self.runtime).qdisc_show(proxy) if proxy else ""
            )
        return build_verify_result(
            self.root_cause_name,
            delay_configured(output, params.delay_ms),
            {"qdisc": output},
        )

    def recover_fault(self, params):
        if not hasattr(self, "_proxy") and not hasattr(self, "_wire_dev"):
            return build_verify_result(
                self.root_cause_name,
                False,
                {"error": "Missing controller link snapshot."},
            )
        if self.lab_backend == "containerlab":
            controller = HostTcController(self.runtime)
            dev = getattr(self, "_wire_dev", None)
            if dev:
                controller.clear(dev)
            restored = not dev or "netem" not in controller.qdisc(dev)
        else:
            controller = KatharaVdeFaultProxy(self.runtime)
            proxy = getattr(self, "_proxy", None)
            if proxy:
                controller.remove(proxy)
            restored = (
                not proxy
                or controller.discover(params.host_name, self._intf(params)) is None
            )
        return build_verify_result(self.root_cause_name, restored, {})

    def oracle_playbook(self, params):
        intf = self._intf(params)
        ends = _probe_ends(self.net_env, params.host_name, intf)
        if ends is None:
            raise ValueError(
                f"No addressed probe endpoints at {params.host_name}:{intf}"
            )
        src, (far, far_intf) = ends
        identities = self.net_env.machine_identities
        endpoints = {
            NodeRole.HOST,
            NodeRole.SERVER,
            NodeRole.SERVICE,
            NodeRole.CONTROLLER,
        }
        if identities[src].role not in endpoints and identities[far].role in endpoints:
            src = _remote_host(self.net_env, {src, far})
        elif (
            identities[src].role in endpoints and identities[far].role not in endpoints
        ):
            far = _remote_host(self.net_env, {src, far})
            if far:
                far_intf = resolve_default_intf("eth0", self.net_env, far)
        elif (
            identities[src].role not in endpoints
            and params.symptom_host
            and params.peer_host
        ):
            src, far = params.symptom_host, params.peer_host
            far_intf = resolve_default_intf("eth0", self.net_env, far)
        if not src or not far or identities[src].role not in endpoints:
            raise ValueError("Link latency needs an endpoint that can run probes.")
        return OraclePlaybook(
            finding=f"The link at {params.host_name}:{intf} adds excessive delay to transit probes.",
            steps=[
                addr_step(far, far_intf, "delay_peer"),
                shell(
                    src,
                    "ping -n -c 4 -i 0.2 127.0.0.1",
                    "Check local probe processing.",
                    Check(
                        "local replies are prompt",
                        lambda out: (rtt := ping_average(out)) is not None and rtt < 10,
                    ),
                ),
                toward(
                    src,
                    "delay_peer",
                    "traceroute -n -q 1 -w 2 -m 16 {ip}",
                    "Locate where transit RTT rises.",
                    None,
                ),
                toward(
                    src,
                    "delay_peer",
                    "ping -n -c 6 -i 0.2 -W 2 {ip}",
                    "Compare latency across the selected link with local probes.",
                    Check(
                        "cross-link replies have excessive RTT",
                        lambda out: (
                            (rtt := ping_average(out)) is not None
                            and rtt >= params.delay_ms * 0.7
                        ),
                    ),
                    evidence=True,
                ),
                OracleStep(
                    "Check that UDP still delivers above the capacity-bottleneck range.",
                    "iperf_test",
                    {
                        "client_host_name": src,
                        "server_host_name": far,
                        "duration": 10,
                        "client_args": "-u -b 5M -l 1200 -f k -p 5298",
                        "server_args": "-p 5298",
                    },
                    Check(
                        "UDP delivers over 2 Mbit/s despite the excessive RTT",
                        lambda out: (
                            (rate := _receiver_bps(out)) is not None
                            and rate > 2_000_000
                        ),
                    ),
                    evidence=True,
                ),
            ],
        )
