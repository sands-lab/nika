import ipaddress
import json
from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
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


class BGPHijacking(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    symptom_probe = "custom"
    symptom_class = "control_plane"
    root_cause_name: str = "bgp_hijacking"
    description = "An unauthorized BGP prefix is advertised (hijack)."
    TAGS: str = ["bgp"]
    native_router_operations = frozenset({"bgp_advertised_to", "originate_bgp_prefix"})
    INCOMPATIBLE_SCENARIOS = {
        "routeros_simple_bgp": "The injector reads and edits the FRR BGP "
        "configuration, which RouterOS routers do not have."
    }
    supported_backends = ("kathara",)
    BENCHMARK_TARGET_ROLE = "access_routers"

    Params = BGPHijackingParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return access_router_victim(ctx, prefer_site_edge=False)

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
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
            wait_until(lambda: ops.bgp_advertised_to(network), 60, interval_s=5)
            self.logger.info(
                f"Injected BGP hijacking on {params.host_name}: {target_network} "
                f"({ops.profile})."
            )
            return
        match self.lab_backend:
            case "containerlab":
                self.runtime.srl_advertise_prefix(params.host_name, target_network)
                self.logger.info(
                    f"Injected BGP hijacking on {params.host_name}: {target_network} (SRL)."
                )
            case "kathara":
                asn_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
                # Off ``lo`` so the IGP does not carry it: only BGP spreads a hijack.
                # The local-table route keeps answering every address in the
                # prefix, as an address with that prefix on ``lo`` did.
                self.runtime.exec(params.host_name, ENSURE_BGP_ORIGIN_IFACE_CMD)
                self.runtime.exec(
                    params.host_name,
                    f"vtysh -c 'configure terminal' -c 'interface {BGP_ORIGIN_IFACE}' "
                    f"-c 'ip address {target_network}' ",
                )
                self.runtime.exec(
                    params.host_name,
                    f"ip route replace local {target_network} "
                    f"dev {BGP_ORIGIN_IFACE} table local",
                )
                # Behind a prefix-list export policy the operator error includes
                # admitting the prefix there, or the hijack never leaves the router.
                export = (
                    f"-c 'ip prefix-list {params.export_prefix_list} seq 1 permit {target_network}' "
                    if params.export_prefix_list
                    else ""
                )
                self.runtime.exec(
                    params.host_name,
                    f"vtysh -c 'configure terminal' {export}-c 'router bgp {asn_number}' -c 'network {target_network}' -c 'end' -c 'write memory' ",
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
            case "containerlab":
                verified = self.runtime.srl_prefix_advertised(
                    params.host_name, target_network
                )
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "target_network": target_network,
                        "has_advertisement": verified,
                    },
                )
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
