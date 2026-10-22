"""Native RouterOS operations for failures whose target router runs RouterOS.

RouterOS routers boot as VMs inside their containers, so Linux tools run in
the container (``ip``, ``nft``, ``tc`` on ``ethN``) do not change the router's
own state. Failures that change router internals use these helpers to apply
the same root cause natively; link-level faults on container interfaces need
no port.
"""

from __future__ import annotations

from typing import Any

from nika.runtime.base import RuntimeCapabilityError


def is_routeros(net_env: Any, node: str) -> bool:
    """Return whether ``node`` is declared as a RouterOS router."""
    identities = getattr(net_env, "machine_identities", None) or {}
    identity = identities.get(node)
    return identity is not None and "routeros" in identity.capabilities


def ros(runtime: Any, node: str, command: str, *, timeout: float = 30) -> str:
    """Run a RouterOS CLI command through the internal management hop."""
    from nika.service.lab.routeros_api import CLI_COMMAND

    return runtime.exec(node, CLI_COMMAND.format(command=command), timeout=timeout)


def ros_count(runtime: Any, node: str, command: str) -> int:
    """Run ``print count-only``; unreadable output counts as zero."""
    from nika.net_env.utils.routeros.parse import routeros_count

    return routeros_count(ros(runtime, node, command))


def ros_rows(runtime: Any, node: str, command: str) -> list[dict[str, str]]:
    """Run ``print terse`` and parse one property mapping per item."""
    from nika.net_env.utils.routeros.parse import routeros_terse_rows

    return routeros_terse_rows(ros(runtime, node, command))


def reject_routeros(problem: Any, node: str, reason: str) -> None:
    """Refuse a failure that has no native RouterOS equivalent on ``node``."""
    if is_routeros(problem.net_env, node):
        raise RuntimeCapabilityError(
            f"{type(problem).__name__} is not supported on RouterOS router "
            f"{node!r}: {reason}"
        )


def ros_bgp_established(runtime: Any, node: str) -> int:
    """Return the number of Established BGP sessions."""
    return ros_count(
        runtime, node, "/routing/bgp/session/print count-only where established"
    )


def ros_reset_bgp(runtime: Any, node: str) -> None:
    """Hard-reset every BGP session, like ``clear ip bgp *``."""
    ros(
        runtime,
        node,
        # Without a pause RouterOS keeps the sessions it would reset.
        "/routing bgp connection disable [find]; :delay 2s; "
        "/routing bgp connection enable [find]",
    )


BGP_DROP_RULES = tuple(
    (chain, port_match)
    for chain in ("input", "forward", "output")
    for port_match in ("dst-port=179", "src-port=179")
)


def ros_filter_rules(runtime: Any, node: str) -> list[dict[str, str]]:
    return ros_rows(runtime, node, "/ip/firewall/filter/print terse")
