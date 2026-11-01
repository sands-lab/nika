import ipaddress
import json
import re
from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.frr import frr_config_command, frr_save_config
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    all_of,
    contains,
    shell,
    vtysh,
)
from nika.problems.support.benchmark_targets import (
    access_router_victim,
    require_role_member,
)
from nika.net_env.utils.kathara.frr_origin import (
    BGP_ORIGIN_IFACE,
    ENSURE_BGP_ORIGIN_IFACE_CMD,
)
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops
from nika.runtime.base import RuntimeCapabilityError
from nika.utils.logger import system_logger

# ==================================================================
# Problem: BGP hijacking problem.
# ==================================================================


class BGPHijackingParams(BaseModel):
    """Parameters for injecting a BGP hijacking fault."""

    host_name: str = Field(description="Target router host name.")
    target_network: Optional[str] = Field(
        default=None,
        description="Network prefix to advertise. Defaults to runtime selection.",
    )
    symptom_host: Optional[str] = Field(
        default=None,
        description="Optional remote router that should learn the hijacked prefix.",
    )
    probe_dst_ip: Optional[str] = Field(
        default=None,
        description="Optional address inside the hijacked prefix for symptom probes.",
    )
    peer_host: Optional[str] = Field(
        default=None,
        description="Optional stub host attached to the hijacking router.",
    )
    observer_host: Optional[str] = Field(
        default=None,
        description=(
            "Optional end host routed through symptom_host whose traffic should "
            "follow the hijack. Defaults to the ISP stub host of symptom_host."
        ),
    )
    export_prefix_list: Optional[str] = Field(
        default=None,
        description=(
            "Optional prefix-list of the hijacker's outbound BGP policy that the "
            "misconfiguration also extends with the target, letting it out."
        ),
    )


def _bgp_paths(output: str) -> list[tuple[str, bool]]:
    """``(origin, best)`` per path of ``show bgp ipv4 unicast <prefix>`` output.

    The origin is ``AS<n>`` (last AS of a non-empty AS path) or, for a path
    from the local AS, the originator (or next hop) address.
    """
    paths = []
    for block in re.split(r"\n(?=  \S)", output):
        lines = block.splitlines()
        if len(lines) < 3 or not re.search(r"^\s+Origin ", block, re.M):
            continue
        as_path = [t for t in lines[0].split(",")[0].split() if t.isdigit()]
        if as_path:
            origin = f"AS{as_path[-1]}"
        else:
            originator = re.search(r"Originator: ([\d.]+)", block)
            hop = lines[1].split()[0] if lines[1].split() else ""
            origin = originator.group(1) if originator else hop
        best = bool(re.search(r"^\s+Origin [^\n]*\bbest\b", block, re.M))
        paths.append((origin, best))
    return paths


def _hijacker_ids(output: str) -> set[str]:
    m = re.search(r"^hijacker=(\S*)$", output, re.M)
    return set(m.group(1).split(",")) if m else set()


def _best_from_hijacker(output: str) -> bool:
    """The best path originates at the hijacker (``hijacker=`` line: its AS
    and addresses)."""
    ids = _hijacker_ids(output)
    return any(best and origin in ids for origin, best in _bgp_paths(output))


def _other_origin_seen(output: str) -> bool:
    """Besides its own, the hijacker holds a path from another origin (the
    owner): not its local AS and not one of its ``addr`` lines."""
    asn = re.search(r"local AS number (\d+)", output)
    own = {"0.0.0.0", *re.findall(r"^addr ([^/\s]+)", output, re.M)}
    if asn:
        own.add(f"AS{asn.group(1)}")
    return any(origin not in own for origin, _ in _bgp_paths(output))


def _scenario_targets(net_env) -> list:
    """Hijacks the scenario knows to win over the owner's route, if any."""
    targets = getattr(net_env, "bgp_hijack_targets", None)
    return list(targets()) if targets is not None else []


def _target_params(target) -> dict[str, str]:
    return {
        "host_name": target.hijacker,
        "target_network": target.prefix,
        "symptom_host": target.observer,
        "observer_host": target.observer_host,
        "probe_dst_ip": target.probe_ip,
    }


class BGPHijacking(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_hijacking"
    description = "An unauthorized BGP prefix is advertised (hijack)."
    TAGS: str = ["bgp"]
    native_router_operations = frozenset({"bgp_advertised_to", "originate_bgp_prefix"})
    INCOMPATIBLE_SCENARIOS = {
        "min3clos": "The fabric's eBGP sessions have no route policies, so peers "
        "accept no BGP routes and an originated prefix never leaves the router.",
        "routeros_simple_bgp": "The injector reads and edits the FRR BGP "
        "configuration, which RouterOS routers do not have.",
    }
    # Kathara, plus Containerlab platforms whose provider originates the
    # prefix (SONiC). SR Linux has no implementation.
    supported_backends = None
    BENCHMARK_TARGET_ROLE = "access_routers"

    Params = BGPHijackingParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        targets = _scenario_targets(ctx.net_env)
        if targets:
            return _target_params(ctx.rng.choice(targets))
        return access_router_victim(ctx, prefer_site_edge=False)

    @classmethod
    def benchmark_inject_options(cls, ctx, base):
        targets = _scenario_targets(ctx.net_env)
        if targets:
            return [_target_params(target) for target in targets]
        return super().benchmark_inject_options(ctx, base)

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        targets = _scenario_targets(ctx.net_env)
        if targets:
            wanted = (inject.get("host_name"), inject.get("target_network"))
            if wanted not in {(t.hijacker, t.prefix) for t in targets}:
                raise ValueError(
                    f"bgp_hijacking {wanted!r} does not divert a victim's traffic "
                    f"on {ctx.scenario} (topo_size={ctx.topo_size!r})"
                )
            return
        require_role_member(
            ctx,
            cls.root_cause_name,
            inject.get("host_name"),
            "access_routers",
            reason="has no attached end host",
        )

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def _default_target_network(self) -> str:
        servers = getattr(self.net_env, "servers", None) or {}
        web_servers = servers.get("web", [])
        if web_servers:
            target_host = web_servers[-1]
        elif getattr(self.net_env, "hosts", None):
            target_host = self.net_env.hosts[-1]
        else:
            nodes = self.runtime.list_nodes()
            hosts = sorted(
                name for name in nodes if any(key in name for key in ("client", "pc"))
            )
            if not hosts:
                raise ValueError("Cannot derive hijack target network: no hosts found")
            target_host = hosts[-1]
        iface = (
            "eth1" if any(key in target_host for key in ("client", "pc")) else "eth0"
        )
        target_network = self.runtime.get_host_ip(target_host, iface, with_prefix=True)
        if not target_network:
            target_network = self.runtime.get_host_ip(target_host, with_prefix=True)
        if not target_network:
            raise ValueError(
                f"Cannot derive hijack target network: no IP on {target_host}"
            )
        net = ipaddress.ip_network(target_network, strict=False)
        if net.prefixlen >= 31:
            return str(net)
        if net.prefixlen >= 25:
            return str(net.supernet(new_prefix=24))
        return str(next(net.subnets(new_prefix=25)))

    def root_cause_resources(self, params: BGPHijackingParams):
        return [node_resource(params.host_name)]

    def _prefix_owners(self, prefix: str, hijacker: str) -> list[str]:
        """Routers that legitimately originate ``prefix`` (scenario inventory)."""
        bgp = (getattr(self.net_env, "inventory", None) or {}).get("bgp") or {}
        owners = {
            str(row.get("device"))
            for row in bgp.get("originated") or []
            if row.get("prefix") == prefix and row.get("device") != hijacker
        }
        return sorted(owners)

    def _node_asns(self, nodes: list[str]) -> set[str]:
        """ASNs of ``nodes`` from the scenario's BGP inventory."""
        bgp = (getattr(self.net_env, "inventory", None) or {}).get("bgp") or {}
        return {
            str(row.get("asn"))
            for row in bgp.get("nodes") or []
            if row.get("device") in nodes and row.get("asn") is not None
        }

    def oracle_playbook(self, params: BGPHijackingParams) -> OraclePlaybook:
        hijacker = params.host_name
        prefix = str(
            ipaddress.ip_network(
                params.target_network or self._default_target_network(), strict=False
            )
        )
        owners = self._prefix_owners(prefix, hijacker)
        owner_asns = self._node_asns(owners)
        # Without a known owner, the owner's path on the hijacker shows it.
        owner_seen = (
            []
            if owners
            else [
                Check(
                    f"{hijacker} also holds a path for {prefix} from another origin",
                    _other_origin_seen,
                )
            ]
        )
        steps = []
        if params.observer_host and params.probe_dst_ip:
            steps.append(
                shell(
                    params.observer_host,
                    f"traceroute -n -w 1 -q 1 -m 12 {params.probe_dst_ip} 2>&1 "
                    f"|| ping -c 3 -W 1 {params.probe_dst_ip}",
                    f"Trace where {params.observer_host}'s traffic to "
                    f"{params.probe_dst_ip} ends.",
                    timeout=40,
                )
            )
        steps.append(
            shell(
                hijacker,
                vtysh(f"show bgp ipv4 unicast {prefix}")
                + "; "
                + vtysh("show bgp summary")
                + " | grep -m1 'local AS number'"
                "; ip -o -4 addr show | awk '{print \"addr \" $4}'",
                f"Check whether {hijacker} originates {prefix} into BGP.",
                all_of(
                    contains(
                        r"\bsourced\b[^\n]*\bbest\b",
                        f"{hijacker} sources {prefix} as its best path",
                    ),
                    contains(
                        r"Advertised to peers:\n\s+\d",
                        f"{hijacker} advertises it to its peers",
                    ),
                    *owner_seen,
                ),
                evidence=True,
                save_as="hijacker",
            )
        )
        observer = params.symptom_host
        if observer and observer != hijacker:

            def observer_view(outputs) -> dict:
                text = outputs.get("hijacker", "")
                ids = re.findall(r"^addr ([^/\s]+)", text, re.M)
                asn = re.search(r"local AS number (\d+)", text)
                # The AS identifies the hijacker only when the owner sits in
                # another AS; otherwise match its addresses alone.
                owner_origins = {o for o, _ in _bgp_paths(text)} - {"0.0.0.0"}
                if asn and not (
                    f"AS{asn.group(1)}" in owner_origins or asn.group(1) in owner_asns
                ):
                    ids.append(f"AS{asn.group(1)}")
                command = f"echo hijacker={','.join(ids)}; " + vtysh(
                    f"show bgp ipv4 unicast {prefix}"
                )
                return {"host_name": observer, "command": command, "timeout": 15}

            check = Check(
                f"{observer}'s best path for {prefix} originates at {hijacker}",
                _best_from_hijacker,
            )
            steps.append(
                OracleStep(
                    f"Check which origin {observer} prefers for {prefix}.",
                    "exec_shell",
                    observer_view,
                    check,
                    evidence=True,
                )
            )
        for owner in owners[:1]:
            steps.append(
                shell(
                    owner,
                    vtysh(f"show bgp ipv4 unicast {prefix}"),
                    f"Check the legitimate origin of {prefix} on {owner}.",
                    contains(r"\bsourced\b", f"{owner} originates {prefix} as well"),
                    evidence=True,
                )
            )
        return OraclePlaybook(
            finding=(
                f"{hijacker} originates {prefix}, a prefix it does not own, into BGP "
                "and attracts traffic meant for the legitimate origin."
            ),
            steps=steps,
        )

    def inject_fault(self, params: BGPHijackingParams):
        target_network = (
            params.target_network
            if params.target_network is not None
            else self._default_target_network()
        )
        self._target_network = target_network
        ops = native_fault_ops(self, params.host_name, "originate_bgp_prefix")
        if ops is not None:
            # The provider also admits the prefix through the router's export
            # policy, the native form of ``export_prefix_list``.
            ops.originate_bgp_prefix(target_network)
            # Routers may hold updates for their minimum advertisement
            # interval (30 s for eBGP on IOS XR); inject returns once sent.
            network = str(ipaddress.ip_network(target_network, strict=False))
            if not wait_until(lambda: ops.bgp_advertised_to(network), 60, interval_s=5):
                raise RuntimeError(
                    f"{type(self).__name__}: {ops.profile} on {params.host_name!r} "
                    f"did not advertise hijacked prefix {network}."
                )
            self.logger.info(
                f"Injected BGP hijacking on {params.host_name}: {target_network} "
                f"({ops.profile})."
            )
            return
        match self.lab_backend:
            case "kathara":
                asn_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
                # Off ``lo`` so the IGP does not carry it: only BGP spreads a hijack.
                self.runtime.exec(params.host_name, ENSURE_BGP_ORIGIN_IFACE_CMD)
                # Behind a prefix-list export policy the operator error includes
                # admitting the prefix there, or the hijack never leaves the router.
                export = (
                    [
                        f"ip prefix-list {params.export_prefix_list} seq 1 permit {target_network}"
                    ]
                    if params.export_prefix_list
                    else []
                )
                self.runtime.exec(
                    params.host_name,
                    frr_config_command(
                        f"interface {BGP_ORIGIN_IFACE}",
                        f"ip address {target_network}",
                        "exit",
                        *export,
                        f"router bgp {asn_number}",
                        f"network {target_network}",
                    ),
                )
                frr_save_config(self.runtime, params.host_name)
                # The local-table route keeps answering every address in the
                # prefix, as an address with that prefix on ``lo`` did.
                self.runtime.exec(
                    params.host_name,
                    f"ip route replace local {target_network} "
                    f"dev {BGP_ORIGIN_IFACE} table local",
                )
                self.logger.info(
                    f"Injected BGP hijacking on {params.host_name}: {target_network}."
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )

    def verify_fault(self, params: BGPHijackingParams) -> dict:
        """Verify the router is advertising the hijacked network via BGP."""
        target_network = (
            params.target_network
            if params.target_network is not None
            else getattr(self, "_target_network", None)
            or self._default_target_network()
        )
        ops = native_fault_ops(self, params.host_name, "bgp_advertised_to")
        if ops is not None:
            network = str(ipaddress.ip_network(target_network, strict=False))
            advertised_to = ops.bgp_advertised_to(network)
            return build_verify_result(
                fault_type=self.root_cause_name,
                verified=bool(advertised_to),
                details={
                    "host": params.host_name,
                    "target_network": network,
                    "advertised_to": advertised_to,
                },
            )
        match self.lab_backend:
            case "kathara":
                # The hijacker's BGP table must source the prefix as its best
                # path and send it to a peer; whether peers accept it is the symptom.
                table = self.runtime.exec(
                    params.host_name,
                    f"vtysh -c 'show bgp ipv4 unicast {target_network} json' 2>/dev/null",
                )
                try:
                    entry = json.loads(table) or {}
                except ValueError:
                    entry = {}
                originated = any(
                    path.get("sourced") and (path.get("bestpath") or {}).get("overall")
                    for path in entry.get("paths") or []
                )
                network_prefix = target_network.split("/")[0]
                origin_output = self.runtime.exec(
                    params.host_name, f"ip addr show {BGP_ORIGIN_IFACE} 2>/dev/null"
                ).strip()
                has_origin_ip = network_prefix in origin_output
                advertised_to = sorted(entry.get("advertisedTo") or {})
                verified = originated and has_origin_ip and bool(advertised_to)
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "target_network": target_network,
                        "originated_best": originated,
                        "advertised_to": advertised_to,
                        "has_origin_ip": has_origin_ip,
                    },
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )
