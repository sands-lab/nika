"""Static forwarding-state failure implementations."""

import ipaddress
import re

from pydantic import BaseModel, Field

from nika.problems.support.inject_resolve import (
    resolve_victim_host,
    resolve_victim_host_ip,
)

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
    root_cause_name: str = "host_static_blackhole"
    description = "A static blackhole route drops traffic to a prefix locally."
    TAGS: str = ["bgp"]
    supported_backends = ("kathara", "containerlab")

    Params = StaticBlackHoleParams

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

    def _dataplane_host_ip(self, host: str) -> str | None:
        if hasattr(self.runtime, "get_data_plane_host_ip"):
            ip = self.runtime.get_data_plane_host_ip(host, with_prefix=False)
            if ip:
                return ip
        return self.runtime.get_host_ip(host, with_prefix=False)

    def inject_fault(self, params: StaticBlackHoleParams):
        # Prefer a BGP-learned remote prefix so traffic forwarded by this router
        # toward that prefix is dropped. Local attached subnets often stay
        # on-link and never hit a blackhole installed for them.
        remote_cidr: str | None = None
        match self.lab_backend:
            case "kathara":
                bgp_routes = self.runtime.exec(
                    params.host_name, "ip -4 route show proto bgp 2>/dev/null || true"
                )
                remote_cidr = _first_bgp_prefix(bgp_routes)
            case "containerlab":
                remote_cidr = None
            case _:
                remote_cidr = None

        connected = set(self.runtime.get_connected_devices(params.host_name) or [])
        if remote_cidr is None:
            for host in self._endpoint_hosts():
                if host in connected or host == params.host_name:
                    continue
                cidr = self._dataplane_cidr(host)
                if cidr:
                    remote_cidr = cidr
                    self.victim_device = host
                    break
        if remote_cidr is None:
            self.victim_device = resolve_victim_host(self.runtime, params.host_name)
            host_network = ipaddress.ip_network(
                resolve_victim_host_ip(self.runtime, params.host_name),
                strict=False,
            )
            remote_cidr = str(host_network)
        else:
            host_network = ipaddress.ip_network(remote_cidr, strict=False)
        self._blackhole_network = remote_cidr
        # Persist for verify/probe on a fresh ProblemBase instance.
        self.runtime.exec(
            params.host_name,
            f"printf '%s\\n' '{remote_cidr}' > /tmp/nika_blackhole_network",
        )
        # Prefer probing from a locally attached endpoint through this router.
        local_src = next(
            (
                h
                for h in self._endpoint_hosts()
                + list((self.net_env.servers or {}).get("dns") or [])
                if h in connected
            ),
            None,
        )
        if local_src is None:
            local_src = next(
                (h for h in connected if _is_endpoint_host(h)),
                None,
            )
        # Probe a real host address in the blackholed prefix (not network+2,
        # which is often outside a /31 or an SRL interface that never answers).
        probe_dst = None
        victim = getattr(self, "victim_device", None)
        if victim:
            probe_dst = self._dataplane_host_ip(victim)
        if not probe_dst:
            hosts_in_net = list(host_network.hosts())
            probe_dst = str(
                hosts_in_net[-1] if hosts_in_net else host_network.network_address + 1
            )
        self.runtime.exec(
            params.host_name,
            f"printf '%s\\n' '{probe_dst}' > /tmp/nika_blackhole_dst",
        )
        if local_src:
            self.runtime.exec(
                params.host_name,
                f"printf '%s\\n' '{local_src}' > /tmp/nika_blackhole_src",
            )
        match self.lab_backend:
            case "containerlab":
                self.runtime.srl_add_blackhole_static(
                    params.host_name, self._blackhole_network
                )
            case "kathara":
                self.runtime.exec(
                    params.host_name, f"ip route replace blackhole {host_network}"
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot inject_fault: unsupported backend {backend!r}."
                )
        self.logger.info(
            f"Injected addition of blackhole route {host_network} on {params.host_name}."
        )

    def verify_fault(self, params: StaticBlackHoleParams) -> dict:
        """Verify a blackhole route for the victim's network exists."""
        host_network = getattr(self, "_blackhole_network", None)
        if not host_network:
            stored = self.runtime.exec(
                params.host_name,
                "cat /tmp/nika_blackhole_network 2>/dev/null || true",
            ).strip()
            host_network = stored or None
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
                    params.host_name, "ip route show"
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
