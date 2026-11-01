"""Link-interface failures for the P4 gateway benchmark."""

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import interface_resource
from nika.problems.support.benchmark_targets import (
    p4_gateway_port_target,
    p4_port_options,
)
from nika.problems.support.p4_oracle import (
    bmv2_ports,
    summary_regex,
    int_path_rows,
    int_path_summary,
)
from nika.problems.support.oracle import (
    p4rt,
    Check,
    OraclePlaybook,
    all_of,
    contains,
    shell,
)
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy


class SilentEgressPacketLossParams(BaseModel):
    host_name: str = Field(
        description="Gateway or spine that owns the egress interface."
    )
    intf_name: str = Field(description="Target egress interface.")
    bmv2_port: int = Field(gt=0)
    loss_basis_points: int = Field(default=200, ge=1, le=10000)
    seed: int = 42


class SilentEgressPacketLoss(ProblemBase):
    """A defective egress port silently discards a fraction of its packets.

    The loss sits past the switch pipeline, on the transmit side of the port
    (optics, SerDes, or MAC), in the hardware below anything the switch OS or
    P4Runtime can read. The switch counts the packets out, the link stays up,
    and the peer receives fewer than were sent. Only the port's transmit
    direction loses packets, at random, so only flows hashed onto that uplink
    see low-rate loss.
    """

    failure_domain = FailureDomain.LINK_INTERFACE
    symptom_probe = "custom"
    symptom_class = "gray"
    root_cause_name = "silent_egress_packet_loss"
    description = "Egress silently drops a subset of packets."
    symptom_desc = (
        "A fraction of packets disappears after one healthy egress interface."
    )
    TAGS = ["p4_runtime", "telemetry", "flow_tracking"]
    COMPATIBLE_COLUMNS = frozenset({"p4_dc_gateway"})
    supported_backends = ("kathara",)
    Params = SilentEgressPacketLossParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        if ctx.scenario != "p4_dc_gateway":
            return {"host_name": ctx.host0}
        ctx.rng.choice(ctx.net_env.model.services)  # keep the shared service draw
        params = p4_gateway_port_target(ctx, gateways_only=False)
        params.update(loss_basis_points="200", seed=str(ctx.seed))
        return params

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        return p4_port_options(ctx, base, gateways_only=False)

    def root_cause_resources(self, params: SilentEgressPacketLossParams):
        return [interface_resource(params.host_name, params.intf_name)]

    @staticmethod
    def loss_percent(basis_points: int) -> float:
        return basis_points / 100

    def inject_fault(self, params: SilentEgressPacketLossParams):
        # Lossy transmit path on the wire side of the port, outside every
        # lab node: the proxy drops toward the peer only.
        controller = KatharaVdeFaultProxy(self.runtime)
        self._proxy = controller.insert(params.host_name, params.intf_name)
        controller.set_netem_loss(
            self._proxy, self.loss_percent(params.loss_basis_points)
        )

    def _proxy_state(self, params: SilentEgressPacketLossParams):
        controller = KatharaVdeFaultProxy(self.runtime)
        state = getattr(self, "_proxy", None) or controller.discover(
            params.host_name, params.intf_name
        )
        return controller, state

    def verify_fault(self, params: SilentEgressPacketLossParams) -> dict:
        """Read the loss on the transmit side of the port, outside the switch."""
        controller, state = self._proxy_state(params)
        qdisc = controller.qdisc_show(state) if state is not None else ""
        expected = f"loss {self.loss_percent(params.loss_basis_points):g}%"
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=state is not None
            and state.endpoint.node == params.host_name
            and expected in qdisc,
            details={
                "interface": params.intf_name,
                "bmv2_port": params.bmv2_port,
                "loss_basis_points": params.loss_basis_points,
                "transmit_path": qdisc.strip(),
            },
        )

    def recover_fault(self, params: SilentEgressPacketLossParams) -> dict:
        controller, state = self._proxy_state(params)
        if state is not None:
            controller.remove(state)
        self._proxy = None
        return {
            "verified": controller.discover(params.host_name, params.intf_name) is None,
            "details": {"host": params.host_name, "intf": params.intf_name},
        }

    def oracle_playbook(self, params: SilentEgressPacketLossParams) -> OraclePlaybook:
        switch, intf, port = params.host_name, params.intf_name, params.bmv2_port
        model = self.net_env.model
        device_id = model.switch_info[switch].device_id
        sources = (
            [model.client_on_gateway(switch).name]
            if switch in model.gateways
            else [client.name for client in model.clients]
        )
        services = " ".join(service.ip for service in model.services)
        traffic = [
            shell(
                src,
                f"for i in $(seq 60); do for ip in {services}; do curl -s -o "
                "/dev/null -m 3 -w '%{http_code}\\n' http://$ip/; done; done "
                "| sort | uniq -c",
                f"Open many short HTTP connections from {src} to every service, "
                "so the flows hash across every uplink.",
                timeout=240,
            )
            for src in sources
        ]

        def gray_loss_on_port(output: str) -> bool:
            rows = [r for r in int_path_rows(output) if r[0] == device_id]
            here = [r for r in rows if r[1] == port]
            ok, lost = sum(r[3] for r in here), sum(r[4] for r in here)
            rate = lost / max(1, ok + lost)
            other_ok = sum(r[3] for r in rows if r[1] != port)
            other_lost = sum(r[4] for r in rows if r[1] != port)
            return (
                lost >= 3
                and len({r[2] for r in here if r[4]}) >= 2
                and rate <= 0.2
                and other_ok > 0
                and other_lost < rate * (other_ok + other_lost) / 2
            )

        return OraclePlaybook(
            finding=(
                f"{switch}:{intf} (BMv2 port {port}) silently drops a small "
                "fraction of the packets it transmits: the link is up, the "
                "switch state is intact, and INT traces of flows to several "
                "destinations end at that egress port."
            ),
            steps=[
                shell(
                    switch,
                    f"ip -s link show dev {intf}",
                    f"Check the state and error counters of {switch}:{intf}.",
                    all_of(
                        contains(r"state UP", "the link is up"),
                        contains(r"<[^>]*LOWER_UP", "carrier is present"),
                    ),
                ),
                bmv2_ports(
                    switch,
                    contains(rf"(^|\s){port}@{intf}\b", f"port {port} is {intf}"),
                ),
                p4rt(
                    f"read --switch {switch}",
                    f"Read {switch}'s programmed pipeline, routes, and groups.",
                    contains(
                        summary_regex(
                            switch,
                            r'"pipeline_matches_intent":\s*true,[^{}]*?'
                            r'"mismatches":\s*\[\s*\]',
                        ),
                        f"{switch}'s P4Runtime state matches the intent",
                    ),
                ),
                *traffic,
                int_path_summary(
                    device_id,
                    180,
                    Check(
                        f"a few percent of INT traces to several destinations end "
                        f"at switch_id {device_id} port {port}, at over twice the "
                        "loss rate of the switch's other ports",
                        gray_loss_on_port,
                    ),
                    f"Count delivered and lost INT traces per egress port (switch_id "
                    f"{device_id} is {switch}).",
                    attempts=4,
                    interval_sec=30,
                ),
            ],
        )
