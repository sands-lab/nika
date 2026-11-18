"""IPv4 unicast activation missing from a BGP neighbor."""

from __future__ import annotations

import ipaddress

from pydantic import BaseModel, Field

from nika.net_env.isp.identity import list_isp_base_scenarios
from nika.net_env.utils.routeros.parse import routeros_terse_rows
from nika.problems.base import FailureDomain, ProblemBase, build_verify_result
from nika.problems.rca import node_resource
from nika.problems.support.benchmark_targets import attached_end_hosts, choice
from nika.problems.support.oracle import Check, OraclePlaybook, routeros
from nika.problems.support.polling import wait_until
from nika.problems.support.router_ops import native_fault_ops, native_router_profile
from nika.runtime.base import RuntimeCapabilityError

_SESSION_SECTION = "=== ESTABLISHED BGP SESSIONS ==="


def _ipv4_family_missing(output: str, peer: str | None) -> bool:
    connections, separator, sessions = output.partition(_SESSION_SECTION)
    if not separator:
        return False
    configured = routeros_terse_rows(connections)
    established = routeros_terse_rows(sessions)
    for connection in configured:
        address = connection.get("remote.address", "").split("@")[0].split("/")[0]
        afi = set(connection.get("afi", "").split(","))
        if not address or (peer and address != peer) or not afi - {""} or "ip" in afi:
            continue
        matches = [
            session
            for session in established
            if session.get("remote.address", "").split("@")[0] == address
        ]
        if len(matches) != 1:
            continue
        local = set(matches[0].get("local.afi", "").split(","))
        remote = set(matches[0].get("remote.afi", "").split(","))
        if local - {""} and "ip" not in local and "ip" in remote:
            return True
    return False


class BGPAddressFamilyNotActivatedParams(BaseModel):
    host_name: str = Field(description="Target router host name.")
    neighbor_ip: ipaddress.IPv4Address | None = Field(
        default=None,
        description="Neighbor address. Defaults to an established peer carrying IPv4 routes.",
    )


class BGPAddressFamilyNotActivated(ProblemBase):
    """A BGP neighbor lacks IPv4 unicast activation.

    RFC 4760 section 8 requires both speakers to advertise support for an
    AFI/SAFI before they exchange its multiprotocol routes. Deactivate the
    neighbor's IPv4 unicast family. RouterOS keeps the session Established.
    Removing the final family puts the session in Idle on FRR and IOS XR,
    so those profiles are excluded in the current environments.
    Sources:
    https://www.rfc-editor.org/rfc/rfc4760#section-8
    https://docs.frrouting.org/en/latest/bgp.html#clicmd-neighbor-A.B.C.D-activate
    https://www.cisco.com/c/en/us/td/docs/iosxr/cisco8000/bgp/bgp-config-cisco8000/r-wrapper-core-bgp-configurations/r-bgp-address-family-configuration-and-behavior.html
    https://help.mikrotik.com/docs/spaces/ROS/pages/331612228/routing+bgp
    """

    failure_domain = FailureDomain.ROUTING_CONTROL_PLANE
    root_cause_name = "bgp_address_family_not_activated"
    description = "A BGP neighbor lacks IPv4 unicast address-family activation."
    symptom_desc = "A BGP session exchanges no IPv4 routes."
    effect_protocol = "bgp"
    symptom_probe = "custom"
    symptom_class = "control_plane"
    TAGS = ["bgp"]
    COMPATIBLE_COLUMNS = frozenset(
        {"dc_clos", "enterprise_branch"}
        | {
            f"{scenario}/{mode}"
            for scenario in list_isp_base_scenarios()
            for mode in ("ebgp", "ibgp_rr")
        }
    )
    supported_backends = ("kathara",)
    native_router_operations = frozenset(
        {
            "set_bgp_ipv4_unicast",
            "restore_bgp_ipv4_unicast",
            "bgp_ipv4_unicast_status",
            "bgp_established_peers",
            "bgp_received_prefixes",
        }
    )
    Params = BGPAddressFamilyNotActivatedParams
    RECORDED_ATTRS = ("neighbor_ip",)

    @classmethod
    def is_compatible(cls, target, device_profile=None, *, backend=None):
        from nika.problems.support.router_ops import router_platform_has_provider

        return (
            router_platform_has_provider(device_profile)
            and cls.runs_on_device_profile(device_profile)
            and super().is_compatible(target, device_profile, backend=backend)
        )

    @classmethod
    def runs_on_device_profile(cls, device_profile: str) -> bool:
        # Removing the final family puts FRR and IOS XR sessions in Idle; IOS XR
        # is excluded because its provider does not implement the operations.
        if device_profile == "frr":
            return False
        return super().runs_on_device_profile(device_profile)

    @classmethod
    def _targets(cls, net_env):
        plan = getattr(net_env, "bgp_plan", None)
        if plan is not None:
            routers = {item.device for item in plan.originated}
        else:
            routers = {
                router
                for router in net_env.target_roles().get("routers", [])
                if attached_end_hosts(net_env, router)
            }
        # The inventory's FRR peers have only IPv4 unicast. Removing that
        # final family puts the session in Idle rather than Established.
        return sorted(
            router
            for router in routers
            if (profile := native_router_profile(net_env, router))
            and cls.runs_on_device_profile(profile)
        )

    @classmethod
    def benchmark_inject_params(cls, ctx):
        return {"host_name": choice(ctx.rng, cls._targets(ctx.net_env), ctx.router0)}

    @classmethod
    def validate_benchmark_inject(cls, ctx, inject):
        from nika.problems.support.benchmark_targets import require_node_capability

        host = inject.get("host_name")
        require_node_capability(ctx, cls.root_cause_name, host, "bgp")
        if host not in cls._targets(ctx.net_env):
            raise ValueError(
                f"{cls.root_cause_name} needs a RouterOS router carrying host prefixes"
            )

    def root_cause_resources(self, params):
        return [node_resource(params.host_name)]

    def _native(self, ops, host):
        if ops is None or not self.runs_on_device_profile(ops.profile):
            raise RuntimeCapabilityError(
                f"{self.root_cause_name}: FRR and IOS XR peers become Idle "
                f"when their final address family is removed ({host!r})."
            )
        return ops

    def ipv4_status(self, host: str, peer: str) -> dict:
        ops = self._native(
            native_fault_ops(self, host, "bgp_ipv4_unicast_status"), host
        )
        return ops.bgp_ipv4_unicast_status(peer)

    def ipv4_prefixes(self, host: str, peer: str) -> set[str]:
        ops = self._native(native_fault_ops(self, host, "bgp_received_prefixes"), host)
        return ops.bgp_received_prefixes(peer)

    def record_baseline(self, params) -> dict:
        host = params.host_name
        ops = self._native(native_fault_ops(self, host, "bgp_established_peers"), host)
        peers = ops.bgp_established_peers()
        selected = (
            str(params.neighbor_ip)
            if params.neighbor_ip
            else getattr(self, "neighbor_ip", None)
        )
        candidates = (
            [selected] if selected else sorted(peers, key=ipaddress.IPv4Address)
        )
        observations = {}
        for peer in candidates:
            if peer not in peers:
                continue
            status = self.ipv4_status(host, peer)
            observations[peer] = status
            if (
                not status["configured"]
                or not status["negotiated"]
                or status["received_prefix_count"] <= 0
            ):
                continue
            prefixes = self.ipv4_prefixes(host, peer)
            if not prefixes:
                continue
            self.neighbor_ip = peer
            self._healthy_ipv4 = {
                "host": host,
                "neighbor_ip": peer,
                "prefixes": sorted(prefixes),
                **status,
            }
            return self._healthy_ipv4
        raise RuntimeCapabilityError(
            f"No established IPv4 BGP peer carrying routes on {host!r}: {observations}"
        )

    def inject_fault(self, params):
        baseline = self.record_baseline(params)
        host, peer = params.host_name, baseline["neighbor_ip"]
        ops = self._native(native_fault_ops(self, host, "set_bgp_ipv4_unicast"), host)
        ops.set_bgp_ipv4_unicast(peer, False)

    def verify_fault(self, params):
        peer = getattr(self, "neighbor_ip", None)
        status = self.ipv4_status(params.host_name, peer) if peer else {}
        return build_verify_result(
            fault_type=self.root_cause_name,
            verified=bool(status) and not status["configured"],
            details={"host": params.host_name, "neighbor_ip": peer, **status},
        )

    def oracle_playbook(
        self, params: BGPAddressFamilyNotActivatedParams
    ) -> OraclePlaybook:
        host = params.host_name
        peer = str(params.neighbor_ip) if params.neighbor_ip else None
        return OraclePlaybook(
            finding=(
                f"{host} has an Established BGP session whose local configuration "
                "and advertised capabilities omit IPv4 unicast, although its peer supports it."
            ),
            steps=[
                routeros(
                    host,
                    "/routing/bgp/connection/print terse; "
                    f':put "{_SESSION_SECTION}"; '
                    "/routing/bgp/session/print terse where established=yes",
                    f"Compare {host}'s configured address families with its Established sessions.",
                    Check(
                        "The same peer lacks ip in connection afi and Established session local.afi, "
                        "while remote.afi includes ip. Route filters preserve IPv4 capability negotiation.",
                        lambda output: _ipv4_family_missing(output, peer),
                    ),
                    evidence=True,
                    attempts=12,
                    interval_sec=5,
                ),
            ],
        )

    def recover_fault(self, params):
        baseline = getattr(self, "_healthy_ipv4", None)
        if not baseline:
            return {"verified": False, "details": {"error": "no_healthy_baseline"}}
        host, peer = params.host_name, baseline["neighbor_ip"]
        ops = self._native(
            native_fault_ops(self, host, "restore_bgp_ipv4_unicast"), host
        )
        ops.restore_bgp_ipv4_unicast(peer, baseline["configuration"])
        reading = {}

        def restored():
            # Read routes on every poll; RouterOS session counters can lag.
            routes = self.ipv4_prefixes(host, peer)
            status = self.ipv4_status(host, peer)
            reading.update(status)
            reading["prefixes"] = sorted(routes)
            if (
                not status["configured"]
                or not status["negotiated"]
                or status["state"] != "Established"
                or status["received_prefix_count"] == 0
            ):
                return False
            return set(baseline["prefixes"]) <= routes

        ok = wait_until(restored, 180, interval_s=3)
        return {
            "verified": ok,
            "details": {"host": host, "neighbor_ip": peer, **reading},
        }
