from pydantic import BaseModel, Field

import hashlib
import json
import re

from nika.problems.rca import node_resource
from nika.problems.rca.inventory import iter_link_termination_points, parse_endpoint
from nika.problems.base import (
    FailureDomain,
    build_verify_result,
    ProblemBase,
)
from nika.problems.support.benchmark_targets import endpoint_target
from nika.problems.support.oracle import (
    Check,
    OraclePlaybook,
    OracleStep,
    contains,
    shell,
)
from nika.utils.logger import system_logger

_MAC = r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}"


def _cached_mac_not_owner(output: str) -> bool:
    """The cached MAC differs from every reference MAC for the same neighbor.

    References are fresh ARP replies and a same-subnet peer's cache entry.
    """
    cached_part, _, rest = output.partition("== ARPING")
    cached = re.search(rf"lladdr ({_MAC})", cached_part, re.IGNORECASE)
    references = {
        m.lower()
        for m in re.findall(
            rf"(?:reply from \S+ \[|bytes from |== PEER \S+ lladdr )({_MAC})",
            rest,
            re.IGNORECASE,
        )
    }
    return bool(cached and references) and cached.group(1).lower() not in references


# ==================================================================
# Problem: Arp cache poisoning causing data plane issues.
# ==================================================================


class ArpCachePoisoningParams(BaseModel):
    """Parameters for injecting an ARP cache poisoning fault."""

    host_name: str = Field(description="Target host name.")
    fake_mac: str | None = Field(
        default=None,
        description=(
            "Forged MAC address. Defaults to a unicast address derived from the "
            "host and neighbor, like the address of an unrelated NIC."
        ),
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
    symptom_probe = "path_ping"
    symptom_class = "unreachable"
    dynamic_artifact = True
    root_cause_name: str = "arp_cache_poisoning"
    description = "ARP cache is poisoned with a false MAC mapping."
    TAGS: str = ["arp"]
    supported_backends = ("kathara", "containerlab")

    Params = ArpCachePoisoningParams

    @classmethod
    def benchmark_inject_params(cls, ctx):
        params = endpoint_target(ctx, l2_endpoint=True)
        if ctx.scenario == "llmd_lab":
            # Star L2 lab has no default route; poison the HTTP peer.
            params["target_ip"] = "200.0.0.8"
        return params

    def __init__(self, scenario_name: str | None, **kwargs):
        super().__init__(scenario_name, **kwargs)
        self.logger = system_logger
        self._poisoned_neighbor: str | None = None

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

    def _fake_mac(self, params: ArpCachePoisoningParams, neighbor: str) -> str:
        """Forged MAC: ``params.fake_mac`` or a stable locally administered one.

        A placeholder such as ``00:11:22:33:44:55`` reads as injected on its own.
        """
        if params.fake_mac:
            return params.fake_mac
        digest = bytearray(
            hashlib.sha256(f"{params.host_name}:{neighbor}".encode()).digest()[:6]
        )
        digest[0] = (digest[0] & 0xFC) | 0x02
        return ":".join(f"{byte:02x}" for byte in digest)

    def inject_fault(self, params: ArpCachePoisoningParams):
        gateway, dev = self._arp_target(params)
        self._poisoned_neighbor = gateway
        fake_mac = self._fake_mac(params, gateway)
        original = self.runtime.exec(
            params.host_name, f"ip neigh show {gateway} 2>/dev/null"
        )
        # Clos/P4 install nud permanent GW neigh; arp -s does not reliably override.
        self.runtime.exec(params.host_name, "ip neigh flush nud permanent")
        self.runtime.exec(params.host_name, "ip neigh flush all")
        replace = f"ip neigh replace {gateway} lladdr {fake_mac} nud permanent"
        if dev:
            replace += f" dev {dev}"
        self.runtime.exec(params.host_name, replace)
        if "PERMANENT" not in original:
            # A learned neighbor sits behind a router or bridge port that
            # discards frames for a MAC it does not own: the forged entry alone
            # blackholes the traffic, and the host carries no extra state.
            return
        if self.net_env.ROUTERS_CHECK_DST_MAC:
            # The routers only route frames addressed to their own interface
            # MAC, so the forged entry alone blackholes the traffic.
            return
        # Other static-neighbor fabrics (P4 leaves) forward on L3 alone and
        # ignore eth_dst, so frames to the forged MAC would still arrive. Drop
        # them at the host's egress instead. inet/output cannot see Ethernet
        # headers on locally generated packets; netdev egress can.
        egress_dev = dev or "eth0"
        self.runtime.exec(
            params.host_name,
            "command -v nft >/dev/null 2>&1 || "
            "(apt-get update -qq && DEBIAN_FRONTEND=noninteractive "
            "apt-get install -y -qq nftables >/dev/null 2>&1)",
        )
        self.runtime.exec(
            params.host_name,
            "nft delete table netdev filter 2>/dev/null || true",
        )
        self.runtime.exec(params.host_name, "nft add table netdev filter")
        self.runtime.exec(
            params.host_name,
            "nft 'add chain netdev filter egress "
            f"{{ type filter hook egress device {egress_dev} priority 0 ; }}'",
        )
        self.runtime.exec(
            params.host_name,
            f"nft add rule netdev filter egress ether daddr {fake_mac} drop",
        )

    def verify_fault(self, params: ArpCachePoisoningParams) -> dict:
        """Verify the ARP cache has the fake MAC for the poisoned neighbor."""
        gateway = self._poisoned_neighbor
        if gateway is None:
            gateway, _dev = self._arp_target(params)
        fake_mac = self._fake_mac(params, gateway)
        neigh_output = self.runtime.exec(
            params.host_name, f"ip neigh show | grep -i '{fake_mac}'"
        ).strip()
        gw_entry = ""
        if gateway:
            gw_entry = self.runtime.exec(
                params.host_name, f"ip neigh show {gateway} 2>/dev/null"
            ).strip()
        verified = bool(neigh_output) and (
            not gateway or fake_mac.lower() in gw_entry.lower()
        )
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=verified,
            details={
                "host": params.host_name,
                "gateway": gateway,
                "fake_mac": fake_mac,
                "neigh_entry": neigh_output,
                "gateway_neigh": gw_entry,
            },
        )

    def _same_segment_peer(self, host: str) -> str | None:
        """Another end host attached to the same device as *host* (topology)."""
        candidates = list(self.net_env.hosts or [])
        for bucket in (self.net_env.servers or {}).values():
            candidates += [name for name in bucket or [] if name not in candidates]
        attached: dict[str, set[str]] = {}
        for _key, tps in iter_link_termination_points(self.net_env):
            nodes = {parse_endpoint(str(tp))[0] for tp in tps}
            for node in nodes:
                attached.setdefault(node, set()).update(nodes - {node})
        uplinks = attached.get(host, set())
        for peer in candidates:
            if peer != host and (
                peer in uplinks or attached.get(peer, set()) & uplinks
            ):
                return peer
        return None

    def oracle_playbook(self, params: ArpCachePoisoningParams) -> OraclePlaybook:
        host = params.host_name
        if params.target_ip:
            target = params.target_ip.split("/")[0]
            pick = f"nb={target}"
            label = target
        else:
            pick = "nb=$(ip -4 route show default | awk '{print $3; exit}')"
            label = "the default gateway"
        dev = "dev=$(ip -4 route get $nb | grep -o 'dev [^ ]*' | cut -d' ' -f2)"
        peer = self._same_segment_peer(host)
        steps = [
            shell(
                host,
                "ip -4 route; ip neigh show",
                f"Show routes and the neighbor (ARP) table on {host}.",
            )
        ]
        if peer:
            steps.append(
                shell(
                    peer,
                    f"{pick}; ping -c 1 -W 1 $nb >/dev/null 2>&1; ip neigh show $nb",
                    f"Read the MAC that {peer}, on the same subnet, caches for {label}.",
                    contains(rf"lladdr {_MAC}", f"{peer} resolves {label}"),
                    save_as="peer_neigh",
                )
            )

        def compare(outputs) -> dict:
            command = (
                f"{pick}; {dev}; echo neighbor=$nb dev=$dev; "
                "echo '== CACHED'; ip neigh show $nb; "
                "echo '== ARPING'; arping -c 3 -w 4 -I $dev $nb 2>&1 | head -8"
            )
            match = re.search(rf"lladdr ({_MAC})", outputs.get("peer_neigh", ""))
            if peer and match:
                command += f"; echo '== PEER {peer} lladdr {match.group(1)}'"
            return {"host_name": host, "command": command, "timeout": 20}

        steps.append(
            OracleStep(
                f"Compare {host}'s cached MAC for {label} with the MAC that answers "
                "a fresh ARP request and the MAC a same-subnet peer caches.",
                "exec_shell",
                compare,
                Check(
                    "the cached MAC differs from the neighbor's real MAC",
                    _cached_mac_not_owner,
                ),
                evidence=True,
            )
        )
        return OraclePlaybook(
            finding=(
                f"{host}'s ARP cache maps {label} to a forged MAC that no "
                "neighbor owns, so its frames to that neighbor are lost."
            ),
            steps=steps,
        )
