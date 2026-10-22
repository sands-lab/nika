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

    def save_config(self) -> str:
        """Return the running configuration, to restore it after a failure."""
        self._unsupported("Saving the running configuration")

    def restore_config(self, saved: str) -> None:
        """Replace the running configuration with one returned by ``save_config``."""
        self._unsupported("Restoring the running configuration")


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
    if callable(value) and not name.startswith("_")
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
    # reveals the class without touching a lab.
    provider = type(factory(None, ""))
    return frozenset(
        name
        for name in _OPERATIONS
        if getattr(provider, name) is not getattr(RouterFaultOps, name)
    )


def failure_supports_profile(problem_cls, profile: str) -> bool:
    """Whether a failure can target a router running native ``profile``.

    ``problem_cls.native_router_operations`` lists the operations its native
    path needs; ``None`` means it has none, so it only targets FRR/Linux nodes.
    """
    needed = getattr(problem_cls, "native_router_operations", None)
    return needed is not None and needed <= profile_fault_operations(profile)


def unsupported_native_targets(problem_cls, net_env, nodes) -> dict[str, str]:
    """Map each of ``nodes`` whose native profile cannot run the failure to it."""
    unsupported: dict[str, str] = {}
    for node in nodes:
        profile = native_router_profile(net_env, node)
        if profile is not None and not failure_supports_profile(problem_cls, profile):
            unsupported[node] = profile
    return unsupported
