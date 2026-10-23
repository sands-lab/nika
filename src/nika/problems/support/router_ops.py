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

    def remote_bgp_prefixes(self) -> list[str]:
        """Return the active BGP-learned prefixes the router does not serve itself."""
        self._unsupported("Listing BGP-learned prefixes")

    def originate_bgp_prefix(self, prefix: str) -> None:
        """Answer for ``prefix`` locally and advertise it to every BGP peer."""
        self._unsupported("Originating a BGP prefix")

    def bgp_advertised_to(self, prefix: str) -> list[str]:
        """Return the peers ``prefix`` is advertised to."""
        self._unsupported("Reading BGP advertisements")

    # OSPF

    def ospf_interface_areas(self) -> list[str]:
        """Return the dotted area ID of each OSPF interface binding, in order."""
        self._unsupported("Reading OSPF areas")

    def renumber_ospf_area(self, area: str, new_area: str) -> None:
        """Run the OSPF interfaces of ``area`` in ``new_area`` instead."""
        self._unsupported("Changing an OSPF area")

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

    # Running configuration

    def save_config(self) -> str:
        """Return the running configuration, to restore it after a failure."""
        self._unsupported("Saving the running configuration")

    def restore_config(self, saved: str) -> None:
        """Replace the running configuration with one returned by ``save_config``."""
        self._unsupported("Restoring the running configuration")


def router_fault_ops(problem, node: str) -> RouterFaultOps | None:
    """Return the failure-operations provider for ``node``'s native profile.

    The profile comes from the node capabilities the environment declares.
    Returns ``None`` for nodes without a native router profile.
    """
    from nika.net_env.native_routers import native_platform, native_profiles

    identities = getattr(problem.net_env, "machine_identities", None) or {}
    identity = identities.get(node)
    profiles = sorted(
        set(identity.capabilities if identity else ()) & native_profiles()
    )
    if not profiles:
        return None
    if len(profiles) > 1:
        raise RuntimeCapabilityError(
            f"Node {node!r} declares several native router profiles: {profiles}."
        )
    factory = native_platform(profiles[0]).fault_ops
    if factory is None:
        raise RuntimeCapabilityError(
            f"No failure operations are implemented for {profiles[0]} router {node!r}."
        )
    return factory(problem.runtime, node)


def native_fault_ops(problem, node: str, operation: str) -> RouterFaultOps | None:
    """Return ``node``'s provider if it implements ``operation``, else ``None``.

    Failures whose default implementation predates a native profile keep that
    implementation for providers without the operation, so existing behavior on
    those platforms is unchanged.
    """
    ops = router_fault_ops(problem, node)
    return ops if ops is not None and ops.supports(operation) else None
