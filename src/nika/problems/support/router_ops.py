"""Vendor dispatch for failure operations on native routers.

Failure classes stay vendor-neutral: they ask :func:`router_fault_ops` for the
target node's provider. Nodes without a native router profile (FRR, SR Linux,
Linux) get ``None`` and keep the failure's default implementation. A native
profile supplies its provider through ``NativeRouterPlatform.fault_ops``; any
operation it does not implement raises ``RuntimeCapabilityError``.
"""

from __future__ import annotations

from nika.runtime.base import RuntimeCapabilityError


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
