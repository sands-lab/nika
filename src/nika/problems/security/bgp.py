import ipaddress
from typing import Optional

from pydantic import BaseModel, Field

from nika.problems.root_cause import node_resource
from nika.problems.problem_base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
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


class BGPHijacking(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "bgp_hijacking"
    TAGS: str = ["bgp"]

    Params = BGPHijackingParams

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
        match self.lab_backend:
            case "containerlab":
                self.runtime.srl_advertise_prefix(params.host_name, target_network)
                self.logger.info(
                    f"Injected BGP hijacking on {params.host_name}: {target_network} (SRL)."
                )
            case "kathara":
                asn_number = self.runtime.frr_get_bgp_asn_number(params.host_name)
                self.runtime.exec(
                    params.host_name,
                    f"vtysh -c 'configure terminal' -c 'interface lo' -c 'ip address {target_network}' ",
                )
                self.runtime.exec(
                    params.host_name,
                    f"vtysh -c 'configure terminal' -c 'router bgp {asn_number}' -c 'network {target_network}' -c 'end' -c 'write memory' ",
                )
                self.runtime.exec(params.host_name, "systemctl restart frr")
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
                running_config = self.runtime.exec(
                    params.host_name, "vtysh -c 'show running-config' 2>/dev/null"
                ).strip()
                network_prefix = target_network.split("/")[0]
                has_advertisement = (
                    f"network {target_network}" in running_config
                    or f"network {network_prefix}" in running_config
                )
                lo_output = self.runtime.exec(
                    params.host_name, "ip addr show lo 2>/dev/null"
                ).strip()
                has_lo_ip = network_prefix in lo_output
                verified = has_advertisement and has_lo_ip
                return build_verify_result(
                    fault_type=self.root_cause_name,
                    verified=verified,
                    details={
                        "host": params.host_name,
                        "target_network": target_network,
                        "has_advertisement": has_advertisement,
                        "has_lo_ip": has_lo_ip,
                    },
                )
            case backend:
                raise RuntimeCapabilityError(
                    f"{type(self).__name__} cannot verify_fault: unsupported backend {backend!r}."
                )
