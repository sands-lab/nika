"""Silent, flow-dependent corruption in a forwarding device."""

import json
import re
import shlex
from collections import defaultdict, deque
from collections.abc import Callable

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.rca.inventory import (
    interfaces_for_node,
    kernel_data_interface,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    interface_read,
    compare,
    OracleStep,
    shell,
    service_address_command,
)
from nika.problems.support.router_ops import native_router_profile
from nika.problems.support.benchmark_targets import (
    choice,
    device_interfaces,
    first,
    prefer_named,
    resolve_link_flap_params,
)
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.spec import NodeRole
from nika.traffic.workload import BULK_TRANSIT
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf import (
    SwitchNamespaceBitflip,
    SwitchEgressClassifier,
)


class SwitchInternalPacketCorruptionParams(BaseModel):
    """Controller-only inputs for the deterministic switch bitflip injector."""

    forwarding_device: str = Field(description="Target forwarding device.")
    intf_name: str = Field(
        description="Observation anchor on the forwarding device. The fault affects every data port."
    )
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


def _spine_egress_toward_leaf(net_env, leaf_router: str) -> tuple[str, str] | None:
    """Return spine egress intf toward a leaf router."""
    matches: list[tuple[str, str]] = []
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = [str(ep) for ep in tps]
        if len(endpoints) != 2:
            continue
        for idx, ep in enumerate(endpoints):
            host, _intf = parse_endpoint(ep)
            if host != leaf_router:
                continue
            other_ep = endpoints[1 - idx]
            other_host, other_intf = parse_endpoint(other_ep)
            if other_host.startswith("spine_"):
                matches.append((other_host, other_intf))
    if not matches:
        return None
    return sorted(matches)[0]


def _leaf_router_for_web(net_env, web_host: str) -> tuple[str, str] | None:
    """Return the leaf router and its host-facing intf for a web service."""
    needle = f"{web_host}:"
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = [str(ep) for ep in tps]
        web_ep = next((ep for ep in endpoints if ep.startswith(needle)), None)
        if web_ep is None or len(endpoints) != 2:
            continue
        other = endpoints[0] if endpoints[1] == web_ep else endpoints[1]
        peer_host, peer_intf = parse_endpoint(other)
        if peer_host in set(net_env.routers or []) | set(net_env.switches or []):
            return peer_host, peer_intf
    return None


def _workload_transit_egress(net_env) -> tuple[str, str] | None:
    """Forwarding-device egress that the scenario's bulk business traffic crosses.

    Prefers a device with no end host attached (a spine or core), so the
    affected flows cross the fabric between two edge devices.
    """
    workload = net_env.business_workload()
    if workload is None:
        return None
    forwarding = set(net_env.routers or []) | set(net_env.switches or [])
    hops = sorted(
        (node, intf)
        for node, intf in workload.targets(BULK_TRANSIT)
        if node in forwarding
    )
    if not hops:
        return None
    hosts = set(net_env.hosts or [])
    edge = set()
    for _key, tps in iter_link_termination_points(net_env):
        nodes = {parse_endpoint(str(ep))[0] for ep in tps}
        if nodes & hosts:
            edge |= nodes
    return next((hop for hop in hops if hop[0] not in edge), hops[0])


def _probe_path_corruption_target(ctx) -> dict[str, str]:
    """Pin forwarding-device corruption on the default probe path."""
    scenario, net_env, rng = ctx.scenario, ctx.net_env, ctx.rng
    routers = list(ctx.routers)
    forwarding = routers + list(ctx.switches)
    if not forwarding:
        raise ValueError(
            f"device_forwarding_packet_corruption requires a forwarding device in {scenario}"
        )
    # Draws first: the probe endpoints reuse the link-flap path resolver.
    flap = resolve_link_flap_params(
        scenario,
        net_env,
        rng,
        "kathara",
        host_pool=[],
        host0="",
    )
    params: dict[str, str] = {"seed": str(ctx.seed)}
    if flap.get("probe_dst_ip"):
        params["probe_dst_ip"] = flap["probe_dst_ip"]
    if flap.get("observer_device"):
        params["observer_device"] = flap["observer_device"]

    if scenario == "sdn_l3_clos":
        model = getattr(net_env, "model", None)
        if model is not None and getattr(model, "client_endpoints", None):
            observer = model.client_endpoints()[0]
            victim = next(
                web for web in model.web_endpoints() if web.leaf_id != observer.leaf_id
            )
            web_leaf = f"leaf_{victim.leaf_id}"
            port = model.port_to_peer(web_leaf, victim.name)
            params.update(
                forwarding_device=web_leaf,
                intf_name=port.name if port is not None else "eth0",
                probe_dst_ip=victim.ip,
                observer_device=observer.name,
            )
            return params

    if scenario == "dc_clos":
        web_hosts = list(ctx.servers.get("web") or [])
        web = prefer_named(web_hosts, "web_1", first(web_hosts) or ctx.web0)
        leaf_match = _leaf_router_for_web(net_env, web) if web else None
        if leaf_match is not None:
            leaf_router, _leaf_intf = leaf_match
            spine_match = _spine_egress_toward_leaf(net_env, leaf_router)
            if spine_match is not None:
                spine, spine_intf = spine_match
                params.update(
                    forwarding_device=spine,
                    intf_name=spine_intf,
                )
                return params

    if scenario == "campus_lan":
        core_candidates = [n for n in routers if "router_core" in n] or list(routers)
        target = (
            "router_core_2"
            if "router_core_2" in core_candidates
            else choice(rng, core_candidates, core_candidates[0])
        )
        ifaces = device_interfaces(net_env).get(target) or ["eth0"]
        params.update(
            forwarding_device=target,
            intf_name=ifaces[-1] if ifaces else "eth0",
        )
        return params

    if scenario == "enterprise_branch":
        edge = (
            "hq_edge"
            if "hq_edge" in routers
            else choice(rng, list(routers), routers[0])
        )
        ifaces = device_interfaces(net_env).get(edge) or []
        corp_ifaces = sorted(i for i in ifaces if not i.startswith("wg_"))
        params.update(
            forwarding_device=edge,
            intf_name=corp_ifaces[-1] if corp_ifaces else "eth0",
        )
        return params

    transit = _workload_transit_egress(net_env)
    if transit is not None:
        params.update(forwarding_device=transit[0], intf_name=transit[1])
        return params

    target = choice(rng, forwarding, forwarding[0])
    interfaces = device_interfaces(net_env).get(target) or []
    params.update(
        forwarding_device=target,
        intf_name=interfaces[-1] if interfaces else "eth0",
    )
    return params


def _tcp_probe_pair(net_env, device: str, intf: str) -> tuple[str, str, bool]:
    """(client, web server, upload) whose TCP payload leaves ``device:intf``.

    Nodes reachable from the interface's peer without crossing ``device`` sit
    downstream of the egress. A downstream web server receives client uploads
    through it; otherwise a downstream client downloads through it.
    """
    adjacency: dict[str, set[str]] = defaultdict(set)
    downstream: set[str] = set()
    for _key, tps in iter_link_termination_points(net_env):
        endpoints = [str(ep) for ep in tps]
        nodes = [parse_endpoint(ep)[0] for ep in endpoints]
        if f"{device}:{intf}" in endpoints:
            downstream.update(n for n in nodes if n != device)
        for node in nodes:
            adjacency[node].update(n for n in nodes if n != node)
    queue = list(downstream)
    while queue:
        for nxt in adjacency[queue.pop()] - downstream - {device}:
            downstream.add(nxt)
            queue.append(nxt)
    webs = set((getattr(net_env, "servers", None) or {}).get("web") or [])
    workload = net_env.business_workload()
    if workload:
        webs.update(
            process.node
            for process in workload.processes
            if process.app in {"file_server", "web_service"}
            and "--port" in process.args
        )
    webs = sorted(webs)
    clients = sorted(
        name
        for name, identity in net_env.machine_identities.items()
        if identity.role == NodeRole.HOST
    )
    for web in webs:
        client = next((c for c in clients if c not in downstream), None)
        if web in downstream and client:
            return client, web, True
    web = next((w for w in webs if w not in downstream), None)
    client = next((c for c in clients if c in downstream), None)
    if web and client:
        return client, web, False

    # In a redundant fabric, removing one device need not separate the graph.
    # Choose endpoints whose shortest paths include this directed egress;
    # repeated five-tuples sample the available ECMP paths.
    def distances(start):
        found = {start: 0}
        pending = deque([start])
        while pending:
            node = pending.popleft()
            for peer in adjacency[node] - found.keys():
                found[peer] = found[node] + 1
                pending.append(peer)
        return found

    peers = {
        parse_endpoint(str(tp))[0]
        for _key, tps in iter_link_termination_points(net_env)
        if f"{device}:{intf}" in tps
        for tp in tps
        if parse_endpoint(str(tp))[0] != device
    }
    to_device = distances(device)
    for peer in sorted(peers):
        to_peer = distances(peer)
        for client in clients:
            from_client = distances(client)
            for web in webs:
                if client == web or web not in from_client:
                    continue
                length = from_client[web]
                if to_device.get(client, 10000) + 1 + to_peer.get(web, 10000) == length:
                    return client, web, True
                if to_device.get(web, 10000) + 1 + to_peer.get(client, 10000) == length:
                    return client, web, False
    raise ValueError(f"no client/web pair crosses {device}:{intf}")


def _tcp_service_port(net_env, server: str) -> int:
    workload = net_env.business_workload()
    if workload:
        for process in workload.processes:
            if (
                process.node == server
                and process.app in {"file_server", "web_service"}
                and "--port" in process.args
            ):
                return int(process.args[process.args.index("--port") + 1])
    return 80


def device_probe_pairs(net_env, device: str) -> list[tuple[str, str]]:
    """Two service paths sharing the device, but no data link.

    A single link fault cannot explain degradation on both paths. Designed
    isolation and telemetry attachments are excluded from the path inventory.
    """
    identities = net_env.machine_identities
    graph = defaultdict(set)
    for key, ends in iter_link_termination_points(net_env):
        nodes = [parse_endpoint(str(end))[0] for end in ends]
        if len(nodes) >= 2 and all(
            identities[n].role != NodeRole.INFRASTRUCTURE for n in nodes
        ):
            if len(nodes) == 2:
                graph[nodes[0]].add(nodes[1])
                graph[nodes[1]].add(nodes[0])
            else:
                segment = ("segment", key)
                for node in nodes:
                    graph[node].add(segment)
                    graph[segment].add(node)

    def shortest_path(start, end):
        parents = {start: None}
        pending = deque([start])
        while pending:
            node = pending.popleft()
            if node == end:
                path = [end]
                while parents[path[-1]] is not None:
                    path.append(parents[path[-1]])
                return path[::-1]
            for peer in sorted(graph[node] - parents.keys(), key=str):
                parents[peer] = node
                pending.append(peer)
        return []

    servers = set((net_env.servers or {}).get("web") or [])
    workload = net_env.business_workload()
    if workload:
        servers.update(
            p.node
            for p in workload.processes
            if p.app in {"file_server", "web_service"} and "--port" in p.args
        )
    clients = sorted(
        n
        for n, identity in identities.items()
        if identity.role in {NodeRole.HOST, NodeRole.SERVER, NodeRole.SERVICE}
    )
    intent = net_env.probe_intent()
    isolated = intent.isolation_matcher() if intent else lambda a, b: False
    candidates = []
    for client in clients:
        for server in sorted(servers):
            if isolated(client, server):
                continue
            left = shortest_path(client, device)
            right = shortest_path(device, server)
            direct = shortest_path(client, server)
            if not left or not right or not direct:
                continue
            length = len(direct) - 1
            path = left + right[1:]
            if len(path) != length + 1 or len(set(path)) != len(path):
                continue
            edges = {frozenset((a, b)) for a, b in zip(path, path[1:])}
            candidates.append((length, client, server, edges))
    candidates.sort(key=lambda p: p[:3])
    for index, candidate in enumerate(candidates):
        for second in candidates[index + 1 :]:
            if candidate[3].isdisjoint(second[3]):
                return [(candidate[1], candidate[2]), (second[1], second[2])]
    raise ValueError(f"No two link-disjoint TCP service paths cross {device}")


def _server_ip(outputs, key="corrupt_server") -> str:
    match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)/", outputs.get(key, ""))
    return match.group(1) if match else "0.0.0.0"


def _available_probe_port_start(output: str, flows: int = 24) -> int:
    """Choose a free source-port block below the normal ephemeral range."""
    occupied = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        match = re.match(r"\S+\s+\d+\s+\d+\s+\S+:(\d+)\s", line.strip())
        if match is None:
            raise ValueError("Unrecognized client TCP socket state")
        occupied.add(int(match[1]))
    for first_port in range(24000, 32768 - flows + 1, flows):
        if occupied.isdisjoint(range(first_port, first_port + flows)):
            return first_port
    raise RuntimeError("No free source-port block for the corruption probe")


def _tcp_transfers(
    client: str,
    upload: bool,
    flows: int = 24,
    port: int = 80,
    server_key: str = "corrupt_server",
    source_port_start: int | Callable[[dict], int] | None = None,
) -> OracleStep:
    """Many short TCP transfers, each on a fresh five-tuple."""
    transfer = (
        "head -c 65536 /dev/zero | curl -s -o /dev/null -m 3 --data-binary @- "
        "http://{ip}:{port}/"
        if upload
        else "curl -s -o /dev/null -m 3 -r 0-131071 http://{ip}:{port}/large.bin"
    )

    def args(outputs):
        probe = transfer.format(ip=_server_ip(outputs, server_key), port=port)
        if source_port_start is not None:
            first_port = (
                source_port_start(outputs)
                if callable(source_port_start)
                else source_port_start
            )
            probe = probe.replace("curl ", f"curl --local-port $(({first_port}+i-1)) ")
        command = (
            f"ok=0; failed=0; local_errors=0; for i in $(seq {flows}); do "
            f"if {probe}; "
            "then ok=$((ok+1)); else rc=$?; failed=$((failed+1)); "
            'if [ "$rc" -eq 45 ]; then local_errors=$((local_errors+1)); fi; fi; done; '
            "echo ok=$ok failed=$failed local_errors=$local_errors"
        )
        return {"host_name": client, "command": command, "timeout": flows * 4 + 20}

    direction = "upload to" if upload else "download from"
    return OracleStep(
        f"Run {flows} short TCP transfers from {client} that {direction} the web "
        "server, allowing TCP to recover corrupted packets.",
        "exec_shell",
        args,
        Check(
            "all probe transfers run without local binding errors",
            lambda text: (
                (
                    counts := re.fullmatch(
                        r"\s*ok=(\d+) failed=(\d+) local_errors=0\s*", text
                    )
                )
                is not None
                and int(counts[1]) + int(counts[2]) == flows
            ),
        ),
        evidence=True,
    )


class DeviceForwardingPacketCorruption(ProblemBase):
    """Inject a payload bitflip after a fabric node forwards a TCP packet."""

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "gray"
    root_cause_name = "device_forwarding_packet_corruption"
    CONFUSABLE_WITH = ("link_packet_corruption",)
    description = "A forwarding device silently corrupts selected packets."
    symptom_desc = (
        "A subset of TCP flows through one fabric switch incurs end-to-end "
        "checksum drops and retransmissions while links and BGP remain healthy."
    )
    TAGS = ["forwarding_device"]
    supported_backends = ("kathara", "containerlab")
    Params = SwitchInternalPacketCorruptionParams
    # Egress classifier that carries the fault.
    INJECTOR = SwitchNamespaceBitflip

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def runs_on_device_profile(cls, device_profile: str) -> bool:
        """Containerlab router platforms and RouterOS.

        On Containerlab the classifier sits on the host-side wire end, outside
        the router OS, so any platform works. RouterOS also forwards through
        the Kathara container interface.
        """
        from nika.net_env.isp.profiles import SUPPORTED_BACKEND_PROFILE_PAIRS

        return (
            device_profile == "routeros"
            or ("containerlab", device_profile) in SUPPORTED_BACKEND_PROFILE_PAIRS
        )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = _probe_path_corruption_target(ctx)
        if not (ctx.net_env.servers or {}).get("web"):
            workload = ctx.net_env.business_workload()
            for process in workload.processes if workload else []:
                if process.app not in {"file_server", "web_service"}:
                    continue
                egress = _leaf_router_for_web(ctx.net_env, process.node)
                if egress:
                    device_probe_pairs(ctx.net_env, egress[0])
                    return {
                        **params,
                        "forwarding_device": egress[0],
                        "intf_name": egress[1],
                    }
        try:
            cls.oracle_probe_target(
                ctx.net_env, params["forwarding_device"], params["intf_name"]
            )
            device_probe_pairs(ctx.net_env, params["forwarding_device"])
            return params
        except ValueError:
            for node in sorted(set(ctx.routers) | set(ctx.switches)):
                for intf in sorted(device_interfaces(ctx.net_env).get(node, [])):
                    try:
                        cls.oracle_probe_target(ctx.net_env, node, intf)
                        device_probe_pairs(ctx.net_env, node)
                    except ValueError:
                        continue
                    return {**params, "forwarding_device": node, "intf_name": intf}
            raise ValueError(
                "No forwarding egress carries an observable TCP probe path"
            ) from None

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        target = inject.get("forwarding_device", "")
        target_intf = inject.get("intf_name", "")
        net_env = ctx.net_env
        forwarding_devices = set(net_env.routers or []) | set(net_env.switches or [])
        if target not in forwarding_devices:
            raise ValueError(
                f"{cls.root_cause_name} requires a router or switch target"
            )
        if target_intf not in (ctx.ifaces_by_device.get(target) or []):
            raise ValueError(
                f"forwarding interface {target_intf!r} is not on {target!r}"
            )
        cls.oracle_probe_target(net_env, target, target_intf)
        device_probe_pairs(net_env, target)

    @classmethod
    def oracle_probe_target(cls, net_env, device, intf):
        return _tcp_probe_pair(net_env, device, intf)

    def root_cause_resources(self, params: SwitchInternalPacketCorruptionParams):
        return [node_resource(params.forwarding_device)]

    def _classifier(self) -> SwitchEgressClassifier:
        return self.INJECTOR(self.runtime, self.lab_backend)

    def _device_ports(self, device: str) -> list[str]:
        """Physical data attachments, excluding telemetry infrastructure."""
        identities = self.net_env.machine_identities
        return sorted(
            {
                intf
                for _, ends in iter_link_termination_points(self.net_env)
                if len(ends) >= 2
                and all(
                    identities[parse_endpoint(str(end))[0]].role
                    != NodeRole.INFRASTRUCTURE
                    for end in ends
                )
                for end in ends
                for node, intf in [parse_endpoint(str(end))]
                if node == device
            }
        )

    def inject_fault(self, params: SwitchInternalPacketCorruptionParams) -> None:
        if params.intf_name not in interfaces_for_node(
            self.net_env, params.forwarding_device
        ):
            raise RuntimeCapabilityError(
                f"{params.intf_name!r} is not an interface on {params.forwarding_device!r}"
            )
        identity = self.net_env.machine_identities.get(params.forwarding_device)
        if identity is None or identity.role not in {NodeRole.ROUTER, NodeRole.SWITCH}:
            raise RuntimeCapabilityError(
                f"{self.root_cause_name} requires a router or switch target"
            )
        ports = self._device_ports(params.forwarding_device)
        udp_ports = self._control_udp_ports(params.forwarding_device)
        injector = self._classifier()
        attached = []
        try:
            for intf in ports:
                injector.attach(params.forwarding_device, intf, params.seed, udp_ports)
                attached.append(intf)
        except Exception:
            for intf in reversed(attached):
                injector.detach(params.forwarding_device, intf)
            raise

    def _control_udp_ports(self, device: str) -> set[int]:
        """CPU tunnel transport, from its declared configuration or Linux state."""
        if "wireguard" not in self.net_env.machine_identities[device].capabilities:
            return set()
        if native_router_profile(self.net_env, device) == "routeros":
            config = self.net_env.deployment_configs.get(device, "")
            ports = {
                int(token.removeprefix("listen-port="))
                for line in config.splitlines()
                if line.startswith("/interface wireguard add ")
                for token in shlex.split(line)
                if token.startswith("listen-port=")
            }
            if not ports:
                raise RuntimeCapabilityError(
                    "The environment does not declare native WireGuard transport ports"
                )
            return ports
        output = self.runtime.exec(device, "wg show all listen-port")
        return {
            int(fields[1])
            for line in output.splitlines()
            if len(fields := line.split()) == 2 and fields[1].isdecimal()
        }

    def verify_fault(self, params: SwitchInternalPacketCorruptionParams) -> dict:
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=all(
                self._classifier().attached(params.forwarding_device, intf)
                for intf in self._device_ports(params.forwarding_device)
            ),
            details={
                "forwarding_device": params.forwarding_device,
                "intf": params.intf_name,
            },
        )

    def oracle_playbook(
        self, params: SwitchInternalPacketCorruptionParams
    ) -> OraclePlaybook:
        device = params.forwarding_device
        steps = []
        for index, (client, web) in enumerate(device_probe_pairs(self.net_env, device)):
            steps.extend(
                self._corruption_evidence_steps(
                    client, web, f"corrupt_{index}" if index else "corrupt"
                )
            )
        for intf in self._device_ports(device):
            steps.append(
                interface_read(
                    self.net_env,
                    device,
                    intf,
                    f"Read link state and counters on {device}:{intf}.",
                )
            )
        return OraclePlaybook(
            finding=f"TCP receivers count new checksum errors on two link-disjoint service paths through {device}.",
            steps=steps,
        )

    def _corruption_evidence_steps(
        self, client: str, web: str, key: str
    ) -> list[OracleStep]:
        receiver = web

        def source_port_start(outputs):
            return _available_probe_port_start(outputs[key + "_ports"])

        def capture_args(outputs):
            first_port = source_port_start(outputs)
            return {
                "device": receiver,
                "interface": kernel_data_interface(self.net_env, receiver),
                "capture_filter": f"tcp and src portrange {first_port}-{first_port + 23}",
                "max_duration_sec": 30,
                "max_packets": 2000,
            }

        def capture_id(outputs):
            return {"capture_id": json.loads(outputs[key + "_capture"])["capture_id"]}

        def inspect_probe_packets(outputs):
            return {
                "capture_id": json.loads(outputs[key + "_capture"])["capture_id"],
                "view": "packets",
                "display_filter": "tcp.checksum.status == 0",
                "limit": 1,
            }

        def checksum_delta(outputs):
            return {"host_name": receiver, "command": "nstat -asz TcpInCsumErrors"}

        def increased(counter, before_key):
            def check(output, outputs):
                pattern = rf"{counter}\s+(\d+)"
                before = re.search(pattern, outputs.get(before_key, ""))
                after = re.search(pattern, output)
                return bool(before and after and int(after[1]) > int(before[1]))

            return check

        return [
            shell(
                web,
                service_address_command(self.net_env, web),
                f"Look up the address of web server {web}.",
                save_as=key + "_server",
            ),
            shell(
                receiver,
                "nstat -asz TcpInCsumErrors",
                f"Record the TCP checksum error counter on receiver {receiver}.",
                save_as=key + "_before",
            ),
            shell(
                client,
                "nstat -asz TcpRetransSegs",
                f"Record the TCP retransmission counter on sender {client}.",
                save_as=key + "_retrans_before",
            ),
            shell(
                client,
                "ss -Htan '( sport >= 24000 and sport <= 32767 )'",
                f"Read source ports still owned by TCP sockets on {client}.",
                save_as=key + "_ports",
            ),
            OracleStep(
                f"Capture the probe tuples on receiver {receiver}.",
                "packet_capture_start",
                capture_args,
                save_as=key + "_capture",
            ),
            _tcp_transfers(
                client,
                True,
                port=_tcp_service_port(self.net_env, web),
                server_key=key + "_server",
                source_port_start=source_port_start,
            ),
            OracleStep(
                f"Stop the probe capture on {receiver}.",
                "packet_capture_stop",
                capture_id,
                save_as=key + "_stopped",
            ),
            OracleStep(
                f"Measure new TCP checksum errors on receiver {receiver}.",
                "exec_shell",
                checksum_delta,
                compare(
                    "the transfers produce new TCP checksum errors",
                    increased("TcpInCsumErrors", key + "_before"),
                ),
                evidence=True,
            ),
            OracleStep(
                "Verify that checksum failures belong to the probe tuples, rather than background traffic.",
                "packet_capture_inspect",
                inspect_probe_packets,
                Check(
                    "captured probe packets have invalid TCP checksums",
                    lambda output: json.loads(output).get("total_available", 0) > 0,
                ),
                evidence=True,
            ),
            OracleStep(
                f"Measure new TCP retransmissions on sender {client}.",
                "exec_shell",
                {"host_name": client, "command": "nstat -asz TcpRetransSegs"},
                compare(
                    "the transfers produce new TCP retransmissions",
                    increased("TcpRetransSegs", key + "_retrans_before"),
                ),
                evidence=True,
            ),
        ]

    def recover_fault(self, params: SwitchInternalPacketCorruptionParams) -> dict:
        injector = self._classifier()
        ports = self._device_ports(params.forwarding_device)
        for intf in ports:
            injector.detach(params.forwarding_device, intf)
        return {
            "verified": all(
                not injector.attached(params.forwarding_device, intf) for intf in ports
            ),
            "details": {
                "forwarding_device": params.forwarding_device,
                "intf": params.intf_name,
            },
        }
