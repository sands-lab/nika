"""MikroTik RouterOS as a native router platform for Ethernet environments."""

from __future__ import annotations

from dataclasses import replace

from nika.net_env.native_routers import (
    NativeInterface,
    NativeRouterNode,
    NativeRouterPlatform,
    NativeRouterValidation,
)
from nika.net_env.utils.routeros.common import (
    IMAGE,
    ROUTER_MEMORY_MIB,
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
        per_router=ROUTER_MEMORY_MIB * GIB // 1024,
        hosts=len(env.lab.machines) - routers,
    )


def _node(name: str, config: str) -> NativeRouterNode:
    """Concrete interfaces of a converted router under their container names."""
    import re
    import shlex
    from ipaddress import IPv4Address, IPv4Interface, IPv4Network

    from nika.validation.batfish.routeros_isp import routeros_router_interface
    from nika.validation.batfish.vendor_bgp import vendor_addresses

    areas, networks, table = {}, [], "main"
    for line in config.splitlines():
        parts = dict(p.split("=", 1) for p in shlex.split(line) if "=" in p)
        if line.startswith("/routing bgp instance add "):
            # The design's default-VRF routes live in the BGP instance's table.
            table = parts.get("vrf", "main")
        elif line.startswith("/routing ospf area add "):
            areas[parts["name"]] = parts["area-id"]
        elif line.startswith("/routing ospf interface-template add "):
            networks.append((IPv4Network(parts["networks"]), parts["area"]))
    interfaces = []
    for address, port in vendor_addresses(config, "routeros"):
        if not re.fullmatch(r"ether[0-9]+|br0", port):
            continue  # Tunnel interfaces have no container interface.
        ip = IPv4Interface(address).ip
        area = next((areas[a] for n, a in networks if ip in n), None)
        interfaces.append(
            NativeInterface(
                routeros_router_interface(port),
                str(ip),
                None if area is None else str(IPv4Address(area)),
                port,
            )
        )
    return NativeRouterNode(name, tuple(interfaces), route_tables=(("default", table),))


def _projected_snapshot(project, config: str, shutdown) -> str:
    """Return a projection of the RouterOS router as FRR snapshot content."""
    import re

    from nika.validation.batfish.snapshot import _cumulus_frr_snapshot

    name = re.search(r"^/system identity set name=(\S+)$", config, re.M)
    if name is None:
        raise ValueError("RouterOS projection requires a concrete identity")
    return _cumulus_frr_snapshot(
        name[1],
        "frr version 10.5\nfrr defaults traditional\n"
        + project(name[1], config, shutdown=tuple(shutdown)),
    )


def _batfish_config(config: str, shutdown) -> str:
    """Project the RouterOS router into FRR syntax under its container names."""
    from nika.validation.batfish.routeros_isp import project_routeros_router

    return _projected_snapshot(project_routeros_router, config, shutdown)


def _static_underlay(config: str, shutdown) -> str:
    from nika.validation.batfish.routeros_isp import project_routeros_static_underlay

    return _projected_snapshot(project_routeros_static_underlay, config, shutdown)


def _verify_contract(runtime, contract, plan):
    from nika.net_env.vendor_validation import verify_routeros_contract

    return verify_routeros_contract(runtime, contract, plan)


def _config_command(commands) -> str:
    # The CLI hop single-quotes the script; RouterOS reads double quotes.
    if any("'" in command for command in commands):
        raise ValueError("RouterOS commands cannot contain single quotes")
    return CLI_COMMAND.format(command="; ".join(commands))


def _fault_ops(runtime, node: str):
    from nika.net_env.utils.routeros.fault_ops import RouterOsFaultOps

    return RouterOsFaultOps(runtime, node)


PLATFORM = NativeRouterPlatform(
    convert=convert_frr_routers,
    check_deployable=_check_deployable,
    verify_max_wait_sec=600,
    validation=NativeRouterValidation(
        # The static model analyzes an FRR projection under container names.
        interface=lambda name: name,
        batfish_config=_batfish_config,
        node=_node,
        verify_contract=_verify_contract,
        config_command=_config_command,
        route_inspection_command=CLI_COMMAND.format(command="/ip/route/print detail"),
        static_underlay=_static_underlay,
    ),
    fault_ops=_fault_ops,
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
