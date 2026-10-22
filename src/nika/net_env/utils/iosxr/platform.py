"""IOS XR (XRd Control Plane) as a native router platform for Ethernet labs."""

from __future__ import annotations

import re
import shlex
from ipaddress import IPv4Address, IPv4Interface

from nika.net_env.native_routers import (
    NativeInterface,
    NativeRouterNode,
    NativeRouterPlatform,
    NativeRouterValidation,
)
from nika.net_env.utils.iosxr.common import build_xr_config_command, iosxr_interface
from nika.net_env.utils.iosxr.frr import convert_frr_routers


def _check_deployable(env) -> None:
    from nika.net_env.utils.iosxr.common import IMAGE, require_xrd_image
    from nika.net_env.utils.native_admission import GIB, require_native_lab_memory

    require_xrd_image(IMAGE)
    routers = len(env.native_routers)
    require_native_lab_memory(
        f"{env.LAB_NAME} on IOS XR",
        routers=routers,
        per_router=3 * GIB,
        hosts=len(env.lab.machines) - routers,
    )


def _linux_interface(native: str) -> str:
    if native == "Loopback0":
        return "lo"
    if match := re.fullmatch(r"GigabitEthernet0/0/0/(\d+)", native):
        return f"eth{match[1]}"
    raise ValueError(f"Unsupported IOS XR interface: {native!r}")


def _node(name: str, config: str) -> NativeRouterNode:
    from nika.validation.batfish.vendor_bgp import (
        vendor_addresses,
        vendor_ospf_interfaces,
    )

    areas = vendor_ospf_interfaces(name, config, "iosxr")
    interfaces, loopback = [], None
    for address, native in vendor_addresses(config, "iosxr"):
        ip = str(IPv4Interface(address).ip)
        if native == "Loopback0":
            loopback = ip
            continue
        area = areas.get(native)
        interfaces.append(
            NativeInterface(
                _linux_interface(native),
                ip,
                None if area is None else str(IPv4Address(area[0])),
            )
        )
    return NativeRouterNode(name, tuple(interfaces), loopback)


def _batfish_config(config: str, shutdown) -> str:
    from nika.validation.batfish.vendor_bgp import iosxr_batfish_config

    return iosxr_batfish_config(config, shutdown)


def _verify_contract(runtime, contract, plan):
    from nika.net_env.vendor_validation import verify_iosxr_contract

    return verify_iosxr_contract(runtime, contract, plan)


def _restore_command(running: str) -> str:
    """Replace the running configuration with one read by ``show running-config``."""
    lines = running.splitlines()
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("!! IOS XR")), None
    )
    if start is None or not any(line == "end" for line in lines[start:]):
        raise ValueError("Unrecognized IOS XR running configuration")
    config = "\n".join(lines[start:]) + "\n"
    script = (
        "source /pkg/bin/ztp_helper.sh; "
        "xr_config_file=$(mktemp); trap 'rm -f \"$xr_config_file\"' EXIT; "
        f'printf %s {shlex.quote(config)} > "$xr_config_file"; '
        'xrreplace "$xr_config_file"'
    )
    return shlex.join(["/bin/bash", "-c", script])


PLATFORM = NativeRouterPlatform(
    convert=convert_frr_routers,
    check_deployable=_check_deployable,
    verify_max_wait_sec=900,
    validation=NativeRouterValidation(
        interface=iosxr_interface,
        batfish_config=_batfish_config,
        node=_node,
        verify_contract=_verify_contract,
        config_command=build_xr_config_command,
        running_config_command="/pkg/bin/xr_cli 'show running-config'",
        restore_command=_restore_command,
        route_inspection_command="/pkg/bin/xr_cli 'show route'",
    ),
)


def _bgp_peers():
    from nika.net_env.vendor_validation import (
        IOSXR_BGP_COMMAND,
        iosxr_bgp_established_peers,
    )
    from nika.net_env.verify import BgpPeerQuery

    return BgpPeerQuery(IOSXR_BGP_COMMAND, iosxr_bgp_established_peers)


IOSXR_OSPF_PROCESS = "/pkg/bin/xr_cli 'show ospf'"
IOSXR_OSPF_NEIGHBORS = "/pkg/bin/xr_cli 'show ospf neighbor'"
IOSXR_ROUTES = "/pkg/bin/xr_cli 'show route ipv4'"


def iosxr_ospf_running(output: str) -> bool:
    return "Routing Process" in output


def iosxr_ospf_full_count(output: str) -> int:
    """Count neighbor rows in FULL state of ``show ospf neighbor``."""
    return sum(1 for line in output.splitlines() if "FULL/" in line)


IOSXR_BGP_PEERS = _bgp_peers()
