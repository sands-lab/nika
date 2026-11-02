"""Vendor dispatch for failure operations on native routers.

Failure classes stay vendor-neutral: they ask :func:`router_fault_ops` for the
target node's provider. Nodes without a native router profile (FRR, SR Linux,
Linux) get ``None`` and keep the failure's default implementation. A native
profile supplies its provider through ``NativeRouterPlatform.fault_ops``; any
operation it does not implement raises ``RuntimeCapabilityError``.
"""

from __future__ import annotations

from dataclasses import dataclass

from nika.runtime.base import RuntimeCapabilityError


@dataclass(frozen=True)
class TunnelPeer:
    """The single peer of a point-to-point tunnel interface."""

    public_key: str
    allowed_addresses: tuple[str, ...]
    # Seconds since the last successful handshake; None if there was none.
    handshake_age_sec: float | None


@dataclass(frozen=True)
class SourceNat:
    """A source NAT rule as the router reports it."""

    source_prefix: str
    to_address: str
    to_ports: str | None = None
    protocol: str | None = None


@dataclass(frozen=True)
class OspfNeighbor:
    """An OSPFv2 neighbor on a native interface."""

    router_id: str
    address: str
    interface: str
    state: str


class RouterFaultOps:
    """Failure operations on one native router. Vendors override what they support."""

    profile = "native"

    def __init__(self, runtime, node: str):
        self.runtime = runtime
        self.node = node

    def _unsupported(self, operation: str):
        raise RuntimeCapabilityError(
            f"{operation} is not supported on {self.profile} router {self.node!r}."
        )

    def bgp_asn(self) -> int:
        """Return the running local BGP ASN."""
        self._unsupported("Reading the BGP ASN")

    def set_bgp_asn(self, asn: int) -> None:
        """Replace the local BGP ASN, keeping the rest of the BGP configuration."""
        self._unsupported("Changing the BGP ASN")

    def bgp_networks(self) -> list[str]:
        """Return the prefixes the router originates with BGP ``network`` statements."""
        self._unsupported("Listing BGP networks")

    def withdraw_bgp_network(self, prefix: str) -> None:
        """Remove the BGP ``network`` statement for ``prefix``."""
        self._unsupported("Withdrawing a BGP network")

    def save_config(self) -> str:
        """Return the running configuration, to restore it after a failure."""
        self._unsupported("Saving the running configuration")

    def restore_config(self, saved: str) -> None:
        """Replace the running configuration with one returned by ``save_config``."""
        self._unsupported("Restoring the running configuration")

    def supports(self, operation: str) -> bool:
        """Return whether this provider implements ``operation``."""
        return getattr(type(self), operation) is not getattr(RouterFaultOps, operation)

    # Routing processes and sessions

    def stop_routing_process(self) -> None:
        """Stop the routing process the way a crash would."""
        self._unsupported("Stopping the routing process")

    def bgp_established_peers(self) -> set[str]:
        """Return the addresses of Established BGP peers."""
        self._unsupported("Reading BGP sessions")

    def reset_bgp_session(self, peer: str) -> None:
        """Hard-reset the BGP session to ``peer``, like ``clear ip bgp``."""
        self._unsupported("Resetting a BGP session")

    def bgp_received_prefix_count(self, peer: str) -> int:
        """Return the current IPv4 prefix count received from ``peer``."""
        self._unsupported("Reading received BGP prefixes")

    def set_bgp_ipv4_unicast(self, peer: str, enabled: bool) -> None:
        """Activate or deactivate IPv4 unicast for one BGP neighbor."""
        self._unsupported("Changing BGP IPv4 unicast activation")

    def bgp_ipv4_unicast_status(self, peer: str) -> dict:
        """Read family configuration, IPv4 activation, negotiation, state, and prefix count."""
        self._unsupported("Reading BGP IPv4 unicast activation")

    def restore_bgp_ipv4_unicast(self, peer: str, configuration: str) -> None:
        """Restore the opaque family configuration returned by the status reader."""
        self._unsupported("Restoring BGP IPv4 unicast activation")

    def bgp_prefix_limit(self, peer: str) -> int | None:
        """Return the configured IPv4 prefix limit for ``peer``, if present."""
        self._unsupported("Reading a BGP prefix limit")

    def set_bgp_prefix_limit(self, peer: str, limit: int) -> None:
        """Set a terminating IPv4 prefix limit without an automatic restart."""
        self._unsupported("Setting a BGP prefix limit")

    def bgp_prefix_limit_exceeded(self, peer: str) -> bool:
        """Whether the session is held down because it exceeded its prefix limit."""
        self._unsupported("Reading BGP prefix-limit state")

    def bgp_last_notification(self, peer: str) -> str | None:
        """Return the last received BGP NOTIFICATION reason from ``peer``."""
        self._unsupported("Reading a BGP notification")

    def permit_bgp_prefixes(self, prefix: str, length: int) -> None:
        """Permit more-specifics of ``prefix`` with ``length`` in bound BGP policies."""
        self._unsupported("Permitting BGP prefixes")

    def advertise_bgp_prefixes(self, prefixes: list[str]) -> None:
        """Back the prefixes with discard routes and originate them in BGP."""
        self._unsupported("Advertising BGP prefixes")

    def has_bgp_route(self, prefix: str) -> bool:
        """Whether the IPv4 BGP table has an available path to ``prefix``."""
        self._unsupported("Reading a BGP route")

    def set_bgp_password(self, peer: str, password: str | None) -> None:
        """Set TCP MD5 authentication for one peer, or remove it with ``None``."""
        self._unsupported("Changing a BGP neighbor password")

    def bgp_password_configured(self, peer: str) -> bool:
        """Whether the neighbor has a TCP MD5 password."""
        self._unsupported("Reading a BGP neighbor password")

    def bgp_received_prefixes(self, peer: str) -> set[str]:
        """Return IPv4 prefixes currently learned through the selected BGP peer."""
        self._unsupported("Reading routes from a BGP peer")

    def remote_bgp_prefixes(self) -> list[str]:
        """Return the active BGP-learned prefixes the router does not serve itself."""
        self._unsupported("Listing BGP-learned prefixes")

    def originate_bgp_prefix(self, prefix: str) -> None:
        """Answer for ``prefix`` locally and advertise it to every BGP peer."""
        self._unsupported("Originating a BGP prefix")

    def bgp_advertised_to(self, prefix: str) -> list[str]:
        """Return the peers ``prefix`` is advertised to."""
        self._unsupported("Reading BGP advertisements")

    def drop_bgp_session_policy(self) -> None:
        """Leave every eBGP session without a route policy the router can apply.

        The platform default then rejects every route on those sessions.
        """
        self._unsupported("Removing BGP session policies")

    def bgp_session_policy_dropped(self) -> bool:
        """Whether every eBGP session lacks a route policy the router can apply."""
        self._unsupported("Reading BGP session policies")

    def enable_bgp_ttl_security(self) -> None:
        """Enable one-hop receive TTL security and reset BGP sessions."""
        self._unsupported("Enabling BGP TTL security")

    def bgp_ttl_security_enabled(self) -> bool:
        """Whether every BGP neighbor has one-hop receive TTL security."""
        self._unsupported("Reading BGP TTL security")

    # ------------------------------------------------------------------
    # BGP session reset (GTSM recover)
    # ------------------------------------------------------------------

    def reset_bgp(self) -> None:
        """Reset every BGP session so a recovered TTL-security config takes effect."""
        self._unsupported("Resetting BGP sessions")

    # OSPF

    def ospf_area_ranges(self, area: str) -> dict[str, bool]:
        """Return area ranges mapped to whether they suppress advertisements."""
        self._unsupported("Reading OSPF area ranges")

    def set_ospf_area_ranges(
        self, area: str, prefixes: list[str], suppress: bool
    ) -> None:
        """Add suppressing ranges, or remove those exact ranges."""
        self._unsupported("Changing OSPF area ranges")

    def ospf_summary_lsa_ids(self, area: str) -> set[str]:
        """Return live Type-3 LSA link-state IDs in the specified area."""
        self._unsupported("Reading OSPF summary LSAs")

    def ospf_neighbors(self) -> list[OspfNeighbor]:
        """Return OSPFv2 neighbors, including their adjacency states."""
        self._unsupported("Reading OSPF neighbors")

    def ospf_bindings(self) -> dict[str, tuple[str, ...]]:
        """Map active OSPF binding IDs to the native interfaces they cover."""
        self._unsupported("Reading OSPF bindings")

    def remove_ospf_binding(self, binding: str) -> None:
        """Remove one binding returned by ``ospf_bindings``."""
        self._unsupported("Removing an OSPF binding")

    def ospf_router_id(self) -> str | None:
        """Return the running OSPFv2 router ID."""
        self._unsupported("Reading the OSPF router ID")

    def ospf_router_lsa_ids(self) -> set[str]:
        """Return router-LSA originators in the router's OSPFv2 areas."""
        self._unsupported("Reading OSPF router LSAs")

    def ospf_neighbor_ids(self) -> set[str]:
        """Return the router IDs of OSPFv2 neighbors."""
        self._unsupported("Reading OSPF neighbor IDs")

    def set_ospf_router_id(self, router_id: str) -> None:
        """Set the OSPFv2 router ID and restart OSPF to apply it."""
        self._unsupported("Changing the OSPF router ID")

    def ospf_routes(self) -> set[str]:
        """Return active IPv4 prefixes learned through OSPF."""
        self._unsupported("Reading OSPF routes")

    def connected_routes(self) -> set[str]:
        """Return connected IPv4 subnet prefixes, excluding local host routes."""
        self._unsupported("Reading connected routes")

    def ospf_interface_areas(self) -> list[str]:
        """Return the dotted area ID of each OSPF interface binding, in order."""
        self._unsupported("Reading OSPF areas")

    def renumber_ospf_area(self, area: str, new_area: str) -> None:
        """Run the OSPF interfaces of ``area`` in ``new_area`` instead."""
        self._unsupported("Changing an OSPF area")

    # OSPF interface network type

    def ospf_network_type(self, interface: str) -> str:
        """Return the OSPF network type of ``interface``.

        One of ``broadcast``, ``point-to-point``, or ``non-broadcast``.
        """
        self._unsupported("Reading an OSPF network type")

    def set_ospf_network_type(self, interface: str, network_type: str) -> None:
        """Set the OSPF network type of ``interface``."""
        self._unsupported("Changing an OSPF network type")

    def ospf_interface_timers(self, interface: str) -> tuple[int, int]:
        """Return the OSPF hello and dead intervals in seconds on ``interface``."""
        self._unsupported("Reading OSPF interface timers")

    def set_ospf_interface_timers(self, interface: str, hello: int, dead: int) -> None:
        """Set the OSPF hello and dead intervals on one interface."""
        self._unsupported("Changing OSPF interface timers")

    def ospf_interface_neighbor_full(self, interface: str) -> bool:
        """Whether ``interface`` holds a Full OSPF neighbor."""
        self._unsupported("Reading an OSPF neighbor")

    # ------------------------------------------------------------------
    # IS-IS metric style
    # ------------------------------------------------------------------

    def isis_metric_style(self) -> str:
        """Return the configured IS-IS metric style (``narrow`` or ``wide``)."""
        self._unsupported("Reading the IS-IS metric style")

    def set_isis_metric_style(self, style: str) -> None:
        """Set the IS-IS metric style to ``narrow`` or ``wide``."""
        self._unsupported("Changing the IS-IS metric style")

    # ------------------------------------------------------------------
    # IS-IS level (instance is-type / circuit levels)
    # ------------------------------------------------------------------

    def isis_level(self) -> str:
        """Return the configured IS-IS level.

        One of ``level-1``, ``level-1-2``, or ``level-2-only``.
        """
        self._unsupported("Reading the IS-IS level")

    def set_isis_level(self, level: str) -> None:
        """Set the IS-IS instance (and circuits) to ``level``."""
        self._unsupported("Changing the IS-IS level")

    def isis_hello_authentication(self) -> dict[str, str]:
        """Return hello authentication modes on non-passive IS-IS interfaces."""
        self._unsupported("Reading IS-IS hello authentication")

    def set_isis_hello_password(self, password: str | None) -> None:
        """Set MD5 hello authentication on active circuits, or remove it."""
        self._unsupported("Changing IS-IS hello authentication")

    # Packet filters

    def block_bgp_transport(self) -> None:
        """Drop BGP (TCP 179) in every direction and reset the sessions."""
        self._unsupported("Blocking BGP transport")

    def bgp_transport_blocked(self) -> bool:
        self._unsupported("Reading BGP transport filters")

    def block_icmp_frag_needed(self) -> None:
        """Drop ICMP Fragmentation Needed in every direction."""
        self._unsupported("Blocking ICMP Fragmentation Needed")

    def icmp_frag_needed_blocked(self) -> bool:
        self._unsupported("Reading ICMP filters")

    # Routes and interfaces

    def ipv4_addresses(self) -> set[str]:
        """Return the router's configured IPv4 interface addresses."""
        self._unsupported("Reading IPv4 addresses")

    def add_blackhole_route(self, prefix: str) -> None:
        """Blackhole ``prefix`` in every routing table that carries it."""
        self._unsupported("Adding a blackhole route")

    def blackhole_routes(self) -> list[str]:
        self._unsupported("Listing blackhole routes")

    def route_interface(self, prefix: str) -> str | None:
        """Return the interface of the active route to ``prefix``."""
        self._unsupported("Reading routes")

    def interface_up(self, interface: str) -> bool:
        self._unsupported("Reading interface state")

    def set_interface_mtu(self, interface: str, mtu: int) -> None:
        """Set the router's MTU on container interface ``interface``."""
        self._unsupported("Setting an interface MTU")

    def interface_mtu(self, interface: str) -> int | None:
        self._unsupported("Reading an interface MTU")

    def interface_addresses(self, interface: str) -> list[str]:
        """Return the CIDR addresses on container interface ``interface``."""
        self._unsupported("Reading interface addresses")

    def remove_interface_address(self, interface: str, address: str) -> None:
        self._unsupported("Removing an interface address")

    # Tunnels, QoS marking and NAT

    def tunnel_peer(self, interface: str) -> TunnelPeer:
        self._unsupported("Reading a tunnel peer")

    def set_tunnel_peer_public_key(self, interface: str, key: str) -> None:
        self._unsupported("Changing a tunnel peer key")

    def set_tunnel_peer_allowed_addresses(
        self, interface: str, prefixes: list[str]
    ) -> None:
        self._unsupported("Changing tunnel peer allowed addresses")

    def add_dscp_remark(
        self, interface: str, source_prefix: str, from_dscp: int, to_dscp: int
    ) -> None:
        """Rewrite DSCP before egress classification on ``interface``."""
        self._unsupported("Adding a DSCP remark")

    def dscp_remark_present(self, interface: str, from_dscp: int, to_dscp: int) -> bool:
        self._unsupported("Reading DSCP remarks")

    def remove_dscp_remark(self, interface: str, from_dscp: int, to_dscp: int) -> None:
        self._unsupported("Removing a DSCP remark")

    def vrf_routes(self, vrf: str) -> list[str]:
        """Return the prefixes in a VRF routing table."""
        self._unsupported("Reading VRF routes")

    def add_source_nat(
        self,
        source_prefix: str,
        to_address: str,
        *,
        protocol: str | None = None,
        to_ports: str | None = None,
    ) -> None:
        """Add a source NAT rule ahead of the existing ones."""
        self._unsupported("Adding source NAT")

    def source_nat_rules(self) -> list[SourceNat]:
        self._unsupported("Reading source NAT rules")

    def clear_nat_mappings(self, address: str) -> None:
        """Drop the TCP connection mappings to or from ``address``."""
        self._unsupported("Clearing NAT mappings")

    def nat_mapping_count(self, address: str) -> int:
        """Return the TCP mappings whose reply goes to ``address``."""
        self._unsupported("Reading NAT mappings")


def native_router_profile(net_env, node: str) -> str | None:
    """Return the native router profile ``node`` declares, or ``None``.

    The profile comes from the node capabilities the environment declares;
    FRR, SR Linux and Linux nodes have none.
    """
    from nika.net_env.native_routers import native_profiles

    identities = getattr(net_env, "machine_identities", None) or {}
    identity = identities.get(node)
    profiles = sorted(
        set(identity.capabilities if identity else ()) & native_profiles()
    )
    if len(profiles) > 1:
        raise RuntimeCapabilityError(
            f"Node {node!r} declares several native router profiles: {profiles}."
        )
    return profiles[0] if profiles else None


def router_fault_ops(problem, node: str) -> RouterFaultOps | None:
    """Return the failure-operations provider for ``node``'s native profile.

    Returns ``None`` for nodes without a native router profile.
    """
    from nika.net_env.native_routers import native_platform

    profile = native_router_profile(problem.net_env, node)
    if profile is None:
        return None
    factory = native_platform(profile).fault_ops
    if factory is None:
        raise RuntimeCapabilityError(
            f"No failure operations are implemented for {profile} router {node!r}."
        )
    return factory(problem.runtime, node)


_OPERATIONS = frozenset(
    name
    for name, value in vars(RouterFaultOps).items()
    if callable(value) and not name.startswith("_") and name != "supports"
)


def profile_fault_operations(profile: str) -> frozenset[str]:
    """Return the operations ``profile``'s provider implements.

    An operation is implemented when the provider overrides it; the base
    class only raises ``RuntimeCapabilityError``.
    """
    from nika.net_env.native_routers import native_platform

    factory = native_platform(profile).fault_ops
    if factory is None:
        return frozenset()
    # Providers only bind their runtime and node, so a detached instance
    # answers ``supports`` without touching a lab.
    provider = factory(None, "")
    return frozenset(name for name in _OPERATIONS if provider.supports(name))


def failure_supports_profile(problem_cls, profile: str) -> bool:
    """Whether a failure can target a router running native ``profile``.

    ``problem_cls.native_router_operations`` lists the operations its native
    path needs; ``None`` means it has none, so it only targets FRR/Linux nodes.
    """
    needed = getattr(problem_cls, "native_router_operations", None)
    return needed is not None and needed <= profile_fault_operations(profile)


# Failure parameters that name a node the failure acts on or reads.
TARGET_PARAM_KEYS = (
    "host_name",
    "host_name_2",
    "attacker_device",
    "control_node",
    "node_name",
    "symptom_host",
    "forwarding_device",
)


def target_nodes(params) -> list[str]:
    """Nodes named by a failure's parameters (a model or a mapping)."""
    if params is None:
        return []
    data = params.model_dump() if hasattr(params, "model_dump") else params
    if not isinstance(data, dict):
        return []
    return [str(data[key]) for key in TARGET_PARAM_KEYS if data.get(key)]


def unsupported_native_targets(problem_cls, net_env, nodes) -> dict[str, str]:
    """Map each of ``nodes`` whose native profile cannot run the failure to it."""
    unsupported: dict[str, str] = {}
    for node in nodes:
        profile = native_router_profile(net_env, node)
        if profile is not None and not problem_cls.runs_on_device_profile(profile):
            unsupported[node] = profile
    return unsupported


def native_fault_ops(problem, node: str, operation: str) -> RouterFaultOps | None:
    """Return ``node``'s provider, checked to implement ``operation``.

    Returns ``None`` for nodes without a native router profile, which keep the
    failure's default implementation. A native router never falls back to that
    implementation: if its provider lacks ``operation``, this raises
    ``RuntimeCapabilityError`` before the failure changes anything.
    """
    ops = router_fault_ops(problem, node)
    if ops is not None and not ops.supports(operation):
        raise RuntimeCapabilityError(
            f"{operation} is not supported on {ops.profile} router {node!r}."
        )
    return ops
