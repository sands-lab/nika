"""MikroTik RouterOS as a native router platform for Ethernet environments."""

from __future__ import annotations

from dataclasses import replace

from nika.net_env.native_routers import NativeRouterPlatform
from nika.net_env.utils.routeros.common import (
    CONFIG_LOG_COMMAND,
    IMAGE,
    require_routeros_image,
    routeros_config_applied,
)
from nika.net_env.utils.routeros.frr import convert_frr_routers
from nika.net_env.utils.routeros.parse import (
    routeros_count,
    routeros_established_bgp_peers,
)
from nika.service.lab.routeros_api import CLI_COMMAND


def _check_deployable(env) -> None:
    from nika.net_env.utils.native_admission import GIB, require_native_lab_memory

    require_routeros_image(IMAGE)
    routers = len(env.native_routers)
    require_native_lab_memory(
        f"{env.LAB_NAME} on RouterOS",
        routers=routers,
        per_router=GIB,
        hosts=len(env.lab.machines) - routers,
    )


PLATFORM = NativeRouterPlatform(
    convert=convert_frr_routers,
    check_deployable=_check_deployable,
    verify_max_wait_sec=600,
)


def _with_import_check(command: str) -> str:
    """Prefix a RouterOS query with the startup-import result.

    Before the import, ``/file get`` fails and the query prints nothing, which
    reads as not ready. A rejected import makes the reader raise, so the
    environment fails at once instead of at the verification deadline.
    """
    return CLI_COMMAND.format(
        command=f":put [/file get config.auto.log contents]; {command}"
    )


def _imported(output: str) -> str:
    first, _, rest = output.partition("\n")
    if first.strip():
        routeros_config_applied([("router", first)])
    return rest


def routeros_bgp_peers(output: str) -> set[str]:
    return routeros_established_bgp_peers(_imported(output))


def routeros_bgp_peer_query():
    from nika.net_env.verify import BgpPeerQuery

    return BgpPeerQuery(
        _with_import_check("/routing/bgp/session/print terse where established=yes"),
        routeros_bgp_peers,
    )


ROUTEROS_OSPF_PROCESS = _with_import_check(
    "/routing/ospf/instance/print count-only where !disabled"
)
ROUTEROS_OSPF_NEIGHBORS = CLI_COMMAND.format(
    command="/routing/ospf/neighbor/print count-only where state=Full"
)
ROUTEROS_ROUTES = CLI_COMMAND.format(command="/ip/route/print terse")


def routeros_campus_queries(frr_queries, *, gateways: bool):
    """Campus observations for RouterOS cores, and gateways when converted."""
    queries = replace(
        frr_queries,
        ospf_process=ROUTEROS_OSPF_PROCESS,
        ospf_running=lambda output: routeros_count(_imported(output)) > 0,
        ospf_neighbors=ROUTEROS_OSPF_NEIGHBORS,
        ospf_full_count=routeros_count,
    )
    if not gateways:
        return queries
    return replace(
        queries,
        gateway_addresses=CLI_COMMAND.format(
            command="/ip/address/print terse where interface=br0"
        ),
        dhcp_relay=CLI_COMMAND.format(
            command="/ip/dhcp-relay/print count-only where !disabled and !invalid"
        ),
        dhcp_relay_running=lambda output: routeros_count(output) > 0,
    )


__all__ = [
    "CONFIG_LOG_COMMAND",
    "PLATFORM",
    "ROUTEROS_ROUTES",
    "routeros_bgp_peer_query",
    "routeros_campus_queries",
]
