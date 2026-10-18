"""BGP blackhole route leak retained for the 0.1.0 benchmark cases."""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, Field

from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.inject_resolve import resolve_victim_host_ip


class BGPBlackholeRouteLeakParams(BaseModel):
    host_name: str = Field(description="Router advertising a blackhole route.")


class BGPBlackholeRouteLeak(ProblemBase):
    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "bgp_blackhole_route_leak"
    description = "A BGP router originates a prefix backed by a Null0 route."
    TAGS = ["bgp"]
    supported_backends = ("kathara",)
    Params = BGPBlackholeRouteLeakParams

    def root_cause_resources(self, params: BGPBlackholeRouteLeakParams):
        return [node_resource(params.host_name)]

    def _network(self, router: str) -> str:
        victim_ip = resolve_victim_host_ip(self.runtime, router, with_prefix=False)
        return str(ipaddress.ip_network(f"{victim_ip}/30", strict=False))

    def _asn(self, router: str) -> str:
        return str(self.runtime.frr_get_bgp_asn_number(router))

    def inject_fault(self, params: BGPBlackholeRouteLeakParams):
        network = self._network(params.host_name)
        asn = self._asn(params.host_name)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'configure terminal' "
            f"-c 'ip route {network} Null0' "
            f"-c 'router bgp {asn}' -c 'network {network}' -c 'end'",
        )

    def verify_fault(self, params: BGPBlackholeRouteLeakParams) -> dict:
        network = self._network(params.host_name)
        config = self.runtime.exec(params.host_name, "vtysh -c 'show running-config'")
        static = f"ip route {network} Null0" in config
        advertised = f"network {network}" in config
        return build_verify_result(
            self.root_cause_name,
            static and advertised,
            {
                "router": params.host_name,
                "network": network,
                "static": static,
                "advertised": advertised,
            },
        )

    def recover_fault(self, params: BGPBlackholeRouteLeakParams) -> dict:
        network = self._network(params.host_name)
        asn = self._asn(params.host_name)
        self.runtime.exec(
            params.host_name,
            "vtysh -c 'configure terminal' "
            f"-c 'router bgp {asn}' -c 'no network {network}' "
            f"-c 'exit' -c 'no ip route {network} Null0' -c 'end'",
        )
        config = self.runtime.exec(params.host_name, "vtysh -c 'show running-config'")
        present = (
            f"ip route {network} Null0" in config or f"network {network}" in config
        )
        return build_verify_result(
            self.root_cause_name,
            not present,
            {"router": params.host_name, "network": network, "present": present},
        )
