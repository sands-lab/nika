"""Silent per-flow black hole in a forwarding device."""

from nika.problems.rca import node_resource
from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption import (
    DeviceForwardingPacketCorruption,
    SwitchInternalPacketCorruptionParams,
)
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf import (
    SwitchEgressClassifier,
)


class DeviceFlowBlackholeParams(SwitchInternalPacketCorruptionParams):
    """Forwarding device egress port and the hash seed that picks the dropped flows."""


class SwitchFlowBlackhole(SwitchEgressClassifier):
    SOURCE = "flow_blackhole.bpf.c"


def server_facing_port(ctx) -> tuple[str, str] | None:
    """The forwarding device and port that face a web server.

    Every flow to the server leaves through this port, whichever ECMP path it
    took before, so the fault reaches the business traffic on any host.
    """
    devices = set(ctx.routers) | set(ctx.switches)
    servers = [*(ctx.servers.get("web") or []), ctx.web0]
    for server in dict.fromkeys(s for s in servers if s):
        for _key, tps in iter_link_termination_points(ctx.net_env):
            ends = [parse_endpoint(str(tp)) for tp in tps]
            if len(ends) != 2 or server not in {node for node, _ in ends}:
                continue
            node, intf = next(end for end in ends if end[0] != server)
            if node in devices:
                return node, intf
    return None


class DeviceFlowBlackhole(DeviceForwardingPacketCorruption):
    """Drop every forwarded packet of a hash-selected quarter of five-tuples.

    Pingmesh calls this a packet black hole: a switch deterministically drops
    packets with certain source and destination addresses and ports, while
    the same host pair works over other ports and ping succeeds. Its authors
    suspect errors in ECMP next-hop selection. Guo et al., "Pingmesh",
    SIGCOMM 2015, Section 5.1:
    https://conferences.sigcomm.org/sigcomm/2015/pdf/papers/p139.pdf
    """

    # META is built from the class's own taxonomy fields.
    failure_domain = DeviceForwardingPacketCorruption.failure_domain
    root_cause_name = "device_flow_blackhole"
    description = "A forwarding device silently drops all packets of some flows."
    symptom_desc = (
        "Some TCP and UDP connections across the fabric hang while ping and "
        "routing stay healthy."
    )
    Params = DeviceFlowBlackholeParams
    INJECTOR = SwitchFlowBlackhole

    def root_cause_resources(self, params):
        return [node_resource(params.forwarding_device)]

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = super().benchmark_inject_params(ctx)
        port = server_facing_port(ctx)
        if port is None:
            return params
        return {
            "seed": params["seed"],
            "forwarding_device": port[0],
            "intf_name": port[1],
        }
