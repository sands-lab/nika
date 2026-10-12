"""Static forwarding-state failure implementations."""

import ipaddress
import json
import re

from pydantic import BaseModel, Field

from nika.problems.support.benchmark_targets import (
    access_router_victim,
    require_role_member,
)
from nika.problems.support.inject_resolve import resolve_victim_host_ip

from nika.problems.rca import node_resource

from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)

from nika.runtime.base import RuntimeCapabilityError

from nika.utils.logger import system_logger


class StaticBlackHoleParams(BaseModel):
    """Parameters for injecting a static blackhole route fault."""

    host_name: str = Field(description="Target router host name.")


def _first_bgp_prefix(route_output: str) -> str | None:
    for line in route_output.splitlines():
        match = re.match(r"^(\d+\.\d+\.\d+\.\d+/\d+)\b", line.strip())
        if match:
            return match.group(1)
    return None


def _is_endpoint_host(name: str, *, kind: str = "") -> bool:
    lowered = kind.lower()
    return any(key in name for key in ("client", "pc", "host")) or lowered == "linux"


class StaticBlackHole(ProblemBase):
    failure_domain = FailureDomain.FORWARDING_ENCAPSULATION_POLICY
    symptom_probe = "custom"
    symptom_class = "unreachable"
    root_cause_name: str = "host_static_blackhole"
    description = "A static blackhole route drops traffic to a prefix locally."
    TAGS: str = ["bgp"]
    supported_backends = ("kathara", "containerlab")

    Params = StaticBlackHoleParams

    BENCHMARK_TARGET_ROLE = "access_routers"

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return access_router_victim(ctx, prefer_site_edge=True)

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

    def root_cause_resources(self, params: StaticBlackHoleParams):
        return [node_resource(params.host_name)]

    def _endpoint_hosts(self) -> list[str]:
        """Return client/PC/web endpoints, including Containerlab lab-spec fallback."""
        names: list[str] = []
        seen: set[str] = set()

        def _add(name: str) -> None:
            if name and name not in seen:
                seen.add(name)
                names.append(name)

        for host in list(self.net_env.hosts or []):
            _add(host)
        for host in list((self.net_env.servers or {}).get("web") or []):
            _add(host)

        spec = None
        if hasattr(self.net_env, "get_lab_spec"):
            try:
                spec = self.net_env.get_lab_spec()
            except Exception:  # noqa: BLE001 - inventory optional
                spec = None
        if spec is not None:
            for node in spec.nodes:
                kind = getattr(node, "kind", "") or ""
                if _is_endpoint_host(node.name, kind=kind):
                    _add(node.name)
        return names

    def _dataplane_cidr(self, host: str) -> str | None:
        ip = None
        if hasattr(self.runtime, "get_data_plane_host_ip"):
            ip = self.runtime.get_data_plane_host_ip(host, with_prefix=True)
        if not ip:
            ip = self.runtime.get_host_ip(host, with_prefix=True)
        if not ip:
            return None
        return str(ipaddress.ip_network(ip, strict=False))

    def blackhole_network(self, params: StaticBlackHoleParams) -> str:
        """Return the remote prefix inject blackholes, read from the live router.

        Prefer a BGP-learned remote prefix so traffic forwarded by this router
        toward that prefix is dropped. Local attached subnets often stay
        on-link and never hit a blackhole installed for them.
        """
        if self.lab_backend == "kathara":
            bgp_routes = self.runtime.exec(
                params.host_name, "ip -4 route show proto bgp 2>/dev/null || true"
            )
            remote_cidr = _first_bgp_prefix(bgp_routes)
            if remote_cidr is not None:
                return remote_cidr
        connected = set(self.runtime.get_connected_devices(params.host_name) or [])
        for host in self._endpoint_hosts():
            if host in connected or host == params.host_name:
                continue
            cidr = self._dataplane_cidr(host)
            if cidr:
                return cidr
        return str(
            ipaddress.ip_network(
                resolve_victim_host_ip(self.runtime, params.host_name), strict=False
            )
        )

    def attached_hosts(self, params: StaticBlackHoleParams) -> list[str]:
        """Return the end hosts attached to the router, whose traffic it forwards."""
        connected = set(self.runtime.get_connected_devices(params.host_name) or [])
        candidates = self._endpoint_hosts() + list(
            (self.net_env.servers or {}).get("dns") or []
        )
        local = sorted(set(candidates) & connected)
        return local or sorted(h for h in connected if _is_endpoint_host(h))

    def inject_fault(self, params: StaticBlackHoleParams):
        self._blackhole_network = self.blackhole_network(params)
        host_network = ipaddress.ip_network(self._blackhole_network, strict=False)
        match self.lab_backend:
            case "containerlab":
                self.runtime.srl_add_blackhole_static(
                    params.host_name, self._blackhole_network
                )
            case "kathara":
                # A VRF router forwards end-host traffic from the VRF's table,
                # so the blackhole goes into every table that routes the prefix.
                for table in self._tables_routing(params.host_name, host_network):
                    self.runtime.exec(
                        params.host_name,
                        f"ip route replace blackhole {host_network} table {table}",
                    )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )
        self.logger.info(
            f"Injected addition of blackhole route {host_network} on {params.host_name}."
        )

    def _tables_routing(self, router: str, network) -> list[str]:
        output = self.runtime.exec(
            router, f"ip -j route show table all {network} 2>/dev/null"
        )
        try:
            rows = json.loads(output or "[]")
        except json.JSONDecodeError:
            rows = []
        tables = {str(row.get("table") or "main") for row in rows}
        return sorted(tables | {"main"})

    def verify_fault(self, params: StaticBlackHoleParams) -> dict:
        """Verify a blackhole route for the victim's network exists."""
        host_network = getattr(self, "_blackhole_network", None)
        if not host_network:
            host_network = str(
                ipaddress.ip_network(
                    resolve_victim_host_ip(self.runtime, params.host_name),
                    strict=False,
                )
            )
        match self.lab_backend:
            case "containerlab":
                verified = self.runtime.srl_blackhole_static_present(
                    params.host_name, host_network
                )
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={"host": params.host_name, "network": host_network},
                )
            case "kathara":
                route_output = self.runtime.exec(
                    params.host_name, "ip route show table all type blackhole"
                ).strip()
                verified = (
                    f"blackhole {host_network}" in route_output
                    or f"blackhole {ipaddress.ip_network(host_network, strict=False)}"
                    in route_output
                )
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "network": host_network,
                        "route_output": route_output,
                    },
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )
