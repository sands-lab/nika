"""IOS XR (XRd Control Plane) as a native router platform for Ethernet labs."""

from __future__ import annotations

from nika.net_env.native_routers import NativeRouterPlatform
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


PLATFORM = NativeRouterPlatform(
    convert=convert_frr_routers,
    check_deployable=_check_deployable,
    verify_max_wait_sec=900,
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
