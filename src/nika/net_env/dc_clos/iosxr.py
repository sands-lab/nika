"""Deploy the DC Clos design with Cisco IOS XR (XRd Control Plane) routers."""

from __future__ import annotations

from typing import Literal

from nika.net_env.dc_clos.lab import DCClos, fabric_dimensions

# Each XRd router needs about 2 GiB resident (3 GiB Kathara limit). Size ``m``
# already has 18 routers, which exceeds typical shared hosts.
IOSXR_TOPO_SIZES = ("s",)


class DCClosIosXr(DCClos):
    """The FRR eBGP Clos, its addressing and services, on native IOS XR."""

    ROUTING_PARAMETER_KINDS = frozenset()
    # XRd boots and applies configuration in minutes, not seconds.
    VERIFY_MAX_WAIT_SEC = 900
    VERIFY_RETRY_DELAY_SEC = 10

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        device_profile: str = "iosxr",
        routing_parameters: dict | None = None,
        **kwargs,
    ):
        if device_profile != "iosxr":
            raise ValueError(f"DCClosIosXr deploys iosxr, not {device_profile!r}")
        if topo_size not in IOSXR_TOPO_SIZES:
            super_spines, spines, leaves = fabric_dimensions(topo_size)
            routers = super_spines * (1 + spines + leaves)
            raise ValueError(
                f"dc_clos with device_profile=iosxr supports size "
                f"{', '.join(IOSXR_TOPO_SIZES)} only; size {topo_size!r} has "
                f"{routers} routers at about 2-3 GiB each."
            )
        if routing_parameters is not None:
            raise ValueError(
                "dc_clos with device_profile=iosxr does not bind solved routing "
                "parameters"
            )
        super().__init__(topo_size=topo_size, **kwargs)
        from nika.net_env.utils.iosxr.common import CONFIG_FILE_PATH, IMAGE
        from nika.net_env.utils.iosxr.frr import convert_frr_routers

        self.device_profile = device_profile
        self.deployment_configs = convert_frr_routers(self)
        self.BGP_CONFIG_PATH = CONFIG_FILE_PATH
        self._image = IMAGE
        self.desc += " Routers run Cisco IOS XR (XRd Control Plane)."

    def deploy(self):
        from nika.net_env.utils.iosxr.common import require_xrd_image
        from nika.net_env.utils.native_admission import GIB, require_native_lab_memory

        require_xrd_image(self._image)
        require_native_lab_memory(
            "dc_clos on IOS XR",
            routers=len(self.routers),
            per_router=3 * GIB,
            hosts=len(self.lab.machines) - len(self.routers),
        )
        super().deploy()

    def _bgp_sessions(self):
        from nika.net_env.vendor_validation import (
            IOSXR_BGP_COMMAND,
            iosxr_bgp_established_peers,
        )

        return IOSXR_BGP_COMMAND, iosxr_bgp_established_peers

    def startup_verify_lab(self) -> dict:
        # XR defers best-path selection after sessions establish (BGP update
        # delay), so established sessions alone do not mean routes are ready.
        return self.verify_lab()

    def verify_lab(self) -> dict:
        from nika.net_env.dc_clos.verify import verify_dc_clos_lab

        return verify_dc_clos_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            nodes=tuple(self.lab.machines),
            bgp_neighbors=self.bgp_neighbors,
            client_addresses=self.client_addresses,
            dns_servers=self.dns_servers,
            web_urls=tuple(self.web_urls),
            bgp_sessions=self._bgp_sessions(),
        )
