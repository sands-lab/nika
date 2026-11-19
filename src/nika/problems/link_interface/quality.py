"""Link quality failure implementations."""

import re

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
from nika.problems.link_interface.link import _probe_ends, _hosts_across, _role
from nika.problems.support.oracle import (
    addr_step,
    toward,
    Check,
    compare,
    interface_read,
    OraclePlaybook,
    OracleStep,
)
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from nika.service.containerlab.host_tc import HostTcController

# Receive-side integrity counters: IP header checksum failures and transport
# checksum failures.
_SNMP_CMD = "grep -E '^(Ip|Icmp|Tcp|Udp):' /proc/net/snmp"
_ERROR_FIELDS = {
    "Ip": ("InHdrErrors",),
    "Icmp": ("InCsumErrors",),
    "Tcp": ("InCsumErrors",),
    "Udp": ("InCsumErrors",),
}


def _integrity_errors(output: str) -> int | None:
    """Sum of header/checksum error counters in ``/proc/net/snmp`` text."""
    rows: dict[str, list[list[str]]] = {}
    for line in output.splitlines():
        proto, sep, rest = line.partition(":")
        if sep and proto in _ERROR_FIELDS:
            rows.setdefault(proto, []).append(rest.split())
    total, seen = 0, False
    for proto, fields in _ERROR_FIELDS.items():
        pair = rows.get(proto) or []
        if len(pair) < 2:
            continue
        values = dict(zip(pair[0], pair[1]))
        for name in fields:
            if values.get(name, "").isdigit():
                total += int(values[name])
                seen = True
    return total if seen else None


def _partial_loss(output: str) -> bool:
    match = re.search(r"(\d+(?:\.\d+)?)% packet loss", output)
    return match is not None and 0 < float(match.group(1)) < 100


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
    symptom_probe = "custom"
    symptom_class = "degradation"
    root_cause_name: str = "link_packet_corruption"
    CONFUSABLE_WITH = (
        "device_flow_blackhole",
        "device_forwarding_packet_corruption",
        "silent_egress_packet_loss",
        "link_latency",
    )
    # Goodput collapses only where the path forwards far faster than TCP
    # sustains under the loss; a 1 Mbit/s path is already near that rate.
    minimum_path_rate_kbps = 10_000
    description = "A faulty link corrupts a fraction of the frames it carries."
    TAGS: str = ["link"]
    # Acts on the link outside the router OS: a Kathara proxy or a Containerlab
    # host-side wire end.
    native_router_operations = frozenset()
    BENCHMARK_LINK_PROXY = True
    supported_backends = ("kathara", "containerlab")

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
        if self.lab_backend == "containerlab":
            self._wire_dev = HostTcController(self.runtime).set_corrupt(
                params.host_name, intf_name, params.corruption_percentage
            )
            return
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

    def oracle_playbook(self, params: LinkPacketCorruptionParams) -> OraclePlaybook:
        host = params.host_name
        intf = params.intf_name or select_host_interface(self.net_env, host, last=True)
        steps = [
            interface_read(
                self.net_env, host, intf, f"Inspect the attachment {host}:{intf}."
            )
        ]
        across = _hosts_across(self.net_env, host, intf)
        ends = (
            (across[0], (across[1], "eth0"))
            if across
            else _probe_ends(self.net_env, host, intf)
        )
        if ends is None:
            raise ValueError(
                f"link_packet_corruption on {host}:{intf}: no addressed receiver "
                "beyond the link counts the corrupted frames"
            )
        src, (far, far_intf) = ends

        router_receiver = _role(self.net_env, far) == "router"

        def recount(out):
            return (
                {"host_name": far}
                if router_receiver
                else {"host_name": far, "command": _SNMP_CMD, "timeout": 15}
            )

        steps += [
            addr_step(far, far_intf, "corrupt_peer"),
            OracleStep(
                f"Record protocol integrity counters on {far}.",
                "device_protocol_counters" if router_receiver else "exec_shell",
                {"host_name": far}
                if router_receiver
                else {"host_name": far, "command": _SNMP_CMD},
                Check(
                    "the integrity counters are readable",
                    lambda out: _integrity_errors(out) is not None,
                ),
                save_as="corrupt_snmp",
            ),
            toward(
                src,
                "corrupt_peer",
                "ping -c 200 -i 0.02 -s 1200 -W 1 {ip} | tail -2",
                f"Send 200 large probes from {src} across the link to {far}.",
                Check("some but not all probes are lost", _partial_loss),
                evidence=True,
            ),
            OracleStep(
                f"Read {far}'s checksum error counters again after the probes.",
                "device_protocol_counters" if router_receiver else "exec_shell",
                recount,
                compare(
                    f"{far} counted new IP header or checksum errors on received frames",
                    lambda out, outputs: (
                        _integrity_errors(out) is not None
                        and _integrity_errors(outputs.get("corrupt_snmp", ""))
                        is not None
                        and _integrity_errors(out)
                        > _integrity_errors(outputs["corrupt_snmp"])
                    ),
                ),
                evidence=True,
            ),
        ]
        return OraclePlaybook(
            finding=(
                f"The link on {host}:{intf} corrupts a fraction of the frames it "
                "carries: probes across it are lost and the "
                "receiver counts checksum errors."
            ),
            steps=steps,
        )

    def recover_fault(self, params: LinkPacketCorruptionParams) -> dict:
        """Remove controller-only injection and restore the logical link."""
        intf = self._target_intf(params.host_name, params.intf_name, last=True)
        if self.lab_backend == "containerlab":
            controller = HostTcController(self.runtime)
            dev = getattr(self, "_wire_dev", None) or controller.egress_dev(
                params.host_name, intf
            )
            controller.clear(dev)
            return {
                "verified": not controller.corrupt_configured(dev),
                "details": {"host": params.host_name, "intf": intf},
            }
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
        if self.lab_backend == "containerlab":
            controller = HostTcController(self.runtime)
            dev = getattr(self, "_wire_dev", None) or controller.egress_dev(
                params.host_name, intf
            )
            return controller.corrupt_configured(dev), {
                "host": params.host_name,
                "intf": intf,
                "wire_end": dev,
            }
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
        if self.lab_backend == "containerlab":
            from nika.problems.link_interface.link import _link_peer

            return _link_peer(self.net_env, host_name, intf_name)
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
