from pydantic import BaseModel, Field

import json

from nika.problems.rca import node_resource
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.utils.logger import system_logger

# ==================================================================
# Problem: Arp cache poisoning causing data plane issues.
# ==================================================================


class ArpCachePoisoningParams(BaseModel):
    """Parameters for injecting an ARP cache poisoning fault."""

    host_name: str = Field(description="Target host name.")
    fake_mac: str = Field(
        default="00:11:22:33:44:55", description="Forged MAC address."
    )
    target_ip: str | None = Field(
        default=None,
        description=(
            "Neighbor IP to poison. Defaults to the host's default gateway. "
            "Required on L2-only labs (no default route)."
        ),
    )


class ArpCachePoisoning(ProblemBase):
    failure_domain = FailureDomain.SECURITY
    root_cause_name: str = "arp_cache_poisoning"
    description = "ARP cache is poisoned with a false MAC mapping."
    TAGS: str = ["arp"]

    Params = ArpCachePoisoningParams

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger

    def root_cause_resources(self, params: ArpCachePoisoningParams):
        return [node_resource(params.host_name)]

    def _arp_target(self, params: ArpCachePoisoningParams) -> tuple[str, str | None]:
        """Return (neighbor_ip, device) to poison."""
        if params.target_ip:
            neighbor = params.target_ip.split("/")[0]
            raw = self.runtime.exec(
                params.host_name, f"ip -j route get {neighbor} 2>/dev/null"
            )
            try:
                routes = json.loads(raw or "[]")
            except json.JSONDecodeError:
                routes = []
            dev = None
            if routes:
                dev = routes[0].get("dev")
            return neighbor, dev
        return self._default_route(params.host_name)

    def _default_route(self, host_name: str) -> tuple[str, str | None]:
        """Return (gateway, device) for the default route."""
        raw = self.runtime.exec(host_name, "ip -j route show default 2>/dev/null")
        try:
            routes = json.loads(raw or "[]")
        except json.JSONDecodeError:
            routes = []
        for route in routes:
            if route.get("dst") != "default":
                continue
            gateway = route.get("gateway")
            if gateway:
                return str(gateway), route.get("dev")
        gateway = self.runtime.get_default_gateway(host_name)
        if not gateway:
            raise ValueError(f"No default gateway on {host_name}")
        return gateway, None

    def inject_fault(self, params: ArpCachePoisoningParams):
        gateway, dev = self._arp_target(params)
        # Clos/P4 install nud permanent GW neigh; arp -s does not reliably override.
        self.runtime.exec(params.host_name, "ip neigh flush nud permanent")
        self.runtime.exec(params.host_name, "ip neigh flush all")
        replace = f"ip neigh replace {gateway} lladdr {params.fake_mac} nud permanent"
        if dev:
            replace += f" dev {dev}"
        self.runtime.exec(params.host_name, replace)
        # P2P Clos/P4 leaves match L3 only (ignore eth_dst). inet/output cannot see
        # Ethernet headers on locally generated packets; netdev egress can.
        egress_dev = dev or "eth0"
        self.runtime.exec(
            params.host_name,
            "command -v nft >/dev/null 2>&1 || "
            "(apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y -qq nftables >/dev/null 2>&1)",
        )
        self.runtime.exec(
            params.host_name,
            "nft delete table netdev nika_arp_poison 2>/dev/null || true",
        )
        self.runtime.exec(params.host_name, "nft add table netdev nika_arp_poison")
        self.runtime.exec(
            params.host_name,
            "nft 'add chain netdev nika_arp_poison egress "
            f"{{ type filter hook egress device {egress_dev} priority 0 ; }}'",
        )
        self.runtime.exec(
            params.host_name,
            f"nft add rule netdev nika_arp_poison egress "
            f"ether daddr {params.fake_mac} drop",
        )

    def verify_fault(self, params: ArpCachePoisoningParams) -> dict:
        """Verify the ARP cache has the fake MAC for the poisoned neighbor."""
        gateway, _dev = self._arp_target(params)
        neigh_output = self.runtime.exec(
            params.host_name, f"ip neigh show | grep '{params.fake_mac}'"
        ).strip()
        gw_entry = ""
        if gateway:
            gw_entry = self.runtime.exec(
                params.host_name, f"ip neigh show {gateway} 2>/dev/null"
            ).strip()
        verified = bool(neigh_output) and (
            not gateway or params.fake_mac.lower() in gw_entry.lower()
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "gateway": gateway,
                "fake_mac": params.fake_mac,
                "neigh_entry": neigh_output,
                "gateway_neigh": gw_entry,
            },
        )
