"""Silent per-flow black hole in a forwarding device."""

import json
import re
import shlex

from nika.problems.rca import node_resource
from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption import (
    DeviceForwardingPacketCorruption,
    SwitchInternalPacketCorruptionParams,
    _tcp_probe_pair,
)
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf import (
    SwitchEgressClassifier,
)
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    lacks,
    router_cli,
    shell,
    vtysh,
)
from nika.runtime.spec import NodeRole


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
    # Search the whole web role, not only the RNG `web0` draw. On larger
    # enterprise_branch sizes some web-role PCs sit behind a switch.
    servers = [
        *(ctx.servers.get("web") or []),
        *(ctx.roles.get("web") or []),
        ctx.web0,
    ]
    for server in dict.fromkeys(s for s in servers if s):
        for _key, tps in iter_link_termination_points(ctx.net_env):
            ends = [parse_endpoint(str(tp)) for tp in tps]
            if len(ends) != 2 or server not in {node for node, _ in ends}:
                continue
            node, intf = next(end for end in ends if end[0] != server)
            if node in devices:
                return node, intf
    return None


def _flow_probe_target(net_env, device: str, intf: str) -> tuple[str, str, int]:
    """Choose a declared TCP service across the affected egress port."""
    try:
        client, server, _ = _tcp_probe_pair(net_env, device, intf)
        return client, server, 80
    except ValueError:
        # Workload-owned services need not appear in the legacy web inventory.
        peers = {
            node
            for _, tps in iter_link_termination_points(net_env)
            if f"{device}:{intf}" in tps
            for tp in tps
            if (node := parse_endpoint(str(tp))[0]) != device
        }
        hosts = {
            n
            for n, identity in net_env.machine_identities.items()
            if identity.role == NodeRole.HOST
        }
        for process in net_env.business_workload().processes:
            if (
                process.app not in {"file_server", "web_service"}
                or "--port" not in process.args
            ):
                continue
            clients = hosts - peers if process.node in peers else hosts & peers
            if clients:
                port = int(process.args[process.args.index("--port") + 1])
                return min(clients), process.node, port
        raise ValueError(f"No declared TCP service crosses {device}:{intf}.") from None


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
    # TC egress classifier on the container interface, outside the router OS.
    native_router_operations = frozenset()

    @classmethod
    def runs_on_device_profile(cls, device_profile: str) -> bool:
        # XRd presents forwarded packets as locally originated
        # (ingress_ifindex 0) on the container egress.
        if device_profile == "iosxr":
            return False
        return super().runs_on_device_profile(device_profile)

    def root_cause_resources(self, params):
        return [node_resource(params.forwarding_device)]

    def oracle_playbook(self, params: DeviceFlowBlackholeParams) -> OraclePlaybook:
        device, intf = params.forwarding_device, params.intf_name
        client, web, port = _flow_probe_target(self.net_env, device, intf)

        def probe_args(outputs):
            match = re.search(r"\binet ([0-9.]+)/", outputs["flow_server"])
            if not match:
                raise ValueError("The web server address was not observed.")
            script = f"""
import json, socket, struct
address = {match[1]!r}
results = {{}}
for port in range(20000, 20048):
    outcomes = []
    for repeat in range(2):
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
            sock.settimeout(1.5)
            try:
                sock.bind(('', port))
                sock.connect((address, {port}))
                outcomes.append('connected')
            except TimeoutError:
                outcomes.append('timeout')
            except OSError as error:
                outcomes.append(type(error).__name__)
    results[str(port)] = outcomes
print(json.dumps(results))
"""
            return {
                "host_name": client,
                "command": f"ping -c 3 -W 1 {match[1]}; python3 -c {shlex.quote(script)}",
                "timeout": 180,
            }

        def selective_handshake_loss(output):
            if not re.search(r"\b[1-9]\d* (?:packets )?received", output):
                return False
            results = json.loads(output[output.index("{") :])
            values = list(results.values())
            return (
                len(values) == 48
                and all(v in (["connected"] * 2, ["timeout"] * 2) for v in values)
                and ["connected"] * 2 in values
                and ["timeout"] * 2 in values
            )

        steps = [
            shell(
                web,
                "ip -4 -o addr show scope global",
                f"Read the live address of TCP service host {web}.",
                save_as="flow_server",
            ),
            OracleStep(
                "Repeat TCP handshakes on 48 fixed source ports and compare them with ICMP reachability.",
                "exec_shell",
                probe_args,
                Check(
                    "ICMP succeeds, but identical TCP tuples consistently time out while others connect. Handshake loss distinguishes flow blackholing from payload corruption.",
                    selective_handshake_loss,
                ),
                evidence=True,
                attempts=3,
            ),
            shell(
                device,
                f"tc qdisc show dev {intf}; tc -s filter show dev {intf} egress",
                "Inspect the forwarding container's egress classifier and link impairment configuration.",
                all_of(
                    contains(
                        r"\bbpf\b.*direct-action",
                        "An egress BPF classifier handles the affected flows.",
                    ),
                    lacks(r"\bnetem\b", "No netem impairment explains packet loss."),
                ),
                evidence=True,
            ),
        ]
        if self.net_env.machine_identities[device].role == NodeRole.ROUTER:
            steps.append(
                router_cli(
                    self.net_env,
                    device,
                    "Inspect the forwarding device's routing state through its native CLI.",
                    frr=vtysh("show ip route", "show bgp summary"),
                    iosxr_command="show route ipv4 unicast",
                    routeros_command="/ip route print detail",
                )
            )
        return OraclePlaybook(
            finding=f"{device}:{intf} carries an egress BPF classifier and consistently loses selected TCP handshakes, while other flows and ICMP use the same path successfully.",
            steps=steps,
        )

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
