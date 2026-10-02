"""Silent, flow-dependent corruption in a forwarding device."""

import re

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.rca.inventory import (
    interfaces_for_node,
    iter_link_termination_points,
    parse_endpoint,
)
from nika.problems.support.benchmark_targets import (
    choice,
    device_interfaces,
    first,
    prefer_named,
    resolve_link_flap_params,
)
from nika.net_env.net_env_pool import get_probe_path
from nika.runtime.base import RuntimeCapabilityError
from nika.runtime.spec import NodeRole
from nika.traffic.burst import BurstTrafficGenerator
from nika.problems.forwarding_encapsulation_policy.switch_internal_corruption_bpf import (
    SwitchNamespaceBitflip,
)


class SwitchInternalPacketCorruptionParams(BaseModel):
    """Controller-only inputs for the deterministic switch bitflip injector."""

    forwarding_device: str = Field(description="Target forwarding device.")
    intf_name: str = Field(description="Forwarding device egress interface.")
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
            if other_host.startswith("spine_router_"):
                matches.append((other_host, other_intf))
    if not matches:
        return None
    leaf_match = re.search(r"leaf_router_(\d+)_(\d+)", leaf_router)
    if leaf_match is not None:
        preferred = f"spine_router_{leaf_match.group(1)}_{leaf_match.group(2)}"
        for host, intf in matches:
            if host == preferred:
                return host, intf
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
        if peer_host.startswith("leaf_router_"):
            return peer_host, peer_intf
    return None


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
        web = prefer_named(web_hosts, "webserver0_pod0", first(web_hosts) or ctx.web0)
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

    target = choice(rng, forwarding, forwarding[0])
    interfaces = device_interfaces(net_env).get(target) or []
    params.update(
        forwarding_device=target,
        intf_name=interfaces[-1] if interfaces else "eth0",
    )
    return params


class DeviceForwardingPacketCorruption(ProblemBase):
    """Inject a payload bitflip after a fabric node forwards a TCP packet."""

    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    root_cause_name = "device_forwarding_packet_corruption"
    description = "A forwarding device silently corrupts selected packets."
    symptom_desc = (
        "A subset of TCP flows through one fabric switch incurs end-to-end "
        "checksum drops and retransmissions while links and BGP remain healthy."
    )
    TAGS = ["forwarding_device"]
    supported_backends = ("kathara",)
    Params = SwitchInternalPacketCorruptionParams

    BENCHMARK_TARGETS = "canonical"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return _probe_path_corruption_target(ctx)

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        target = inject.get("forwarding_device", "")
        target_intf = inject.get("intf_name", "")
        net_env = ctx.net_env
        forwarding_devices = set(net_env.routers or []) | set(net_env.switches or [])
        if target not in forwarding_devices:
            raise ValueError(
                "device_forwarding_packet_corruption requires a router or switch target"
            )
        if target_intf not in (ctx.ifaces_by_device.get(target) or []):
            raise ValueError(
                f"forwarding interface {target_intf!r} is not on {target!r}"
            )

    def root_cause_resources(self, params: SwitchInternalPacketCorruptionParams):
        return [node_resource(params.forwarding_device)]

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
                "device_forwarding_packet_corruption requires a router or switch target"
            )
        self._bitflip_token = SwitchNamespaceBitflip(self.runtime).attach(
            params.forwarding_device, params.intf_name, params.seed
        )
        self._start_cross_leaf_workload(params.seed)

    def verify_fault(self, params: SwitchInternalPacketCorruptionParams) -> dict:
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=SwitchNamespaceBitflip(self.runtime).attached(
                params.forwarding_device, params.intf_name
            ),
            details={
                "forwarding_device": params.forwarding_device,
                "intf": params.intf_name,
            },
        )

    def recover_fault(self, params: SwitchInternalPacketCorruptionParams) -> dict:
        injector = SwitchNamespaceBitflip(self.runtime)
        injector.detach(
            params.forwarding_device,
            params.intf_name,
            getattr(self, "_bitflip_token", None),
        )
        # Stop the 60 s cross-leaf burst so the restored path is not congested.
        for host in getattr(self, "_workload_hosts", ()):
            try:
                self.runtime.exec(host, "pkill -f 'iperf3' >/dev/null 2>&1 || true")
            except Exception:  # noqa: BLE001
                pass
        return {
            "verified": not injector.attached(
                params.forwarding_device, params.intf_name
            ),
            "details": {
                "forwarding_device": params.forwarding_device,
                "intf": params.intf_name,
            },
        }

    def _start_cross_leaf_workload(self, seed: int) -> None:
        """Generate stable TCP tuples on the default probe path."""
        scenario = getattr(self, "scenario_name", None) or ""
        topo_size = getattr(self.net_env, "topo_size", None) or "s"
        path = get_probe_path(scenario, topo_size=topo_size)
        if path is not None and path.peer_host and path.peer_host != path.src_host:
            self._workload_hosts = (path.src_host, path.peer_host)
            BurstTrafficGenerator(self.runtime).run(
                sources=[path.src_host],
                destination=path.peer_host,
                protocol="tcp",
                rate="10M",
                packet_size=1200,
                duration=60,
                synchronized_start=0,
                seed=seed,
                flows_per_source=24,
            )
            return
        hosts = sorted(
            name
            for name, identity in self.net_env.machine_identities.items()
            if identity.role is NodeRole.HOST
        )
        if len(hosts) < 2:
            return
        self._workload_hosts = (hosts[0], hosts[-1])
        BurstTrafficGenerator(self.runtime).run(
            sources=[hosts[0]],
            destination=hosts[-1],
            protocol="tcp",
            rate="10M",
            packet_size=1200,
            duration=60,
            synchronized_start=0,
            seed=seed,
            flows_per_source=24,
        )
