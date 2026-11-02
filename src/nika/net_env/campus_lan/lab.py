"""Campus LAN fabric with DHCP, DNS, and load-balanced HTTP services."""

from __future__ import annotations

from typing import Literal

from nika.net_env.base import NetworkEnvBase, ProbePath
from nika.net_env.native_routers import NativeRouterEnv


class CampusLan(NativeRouterEnv, NetworkEnvBase):
    ROUTING_PARAMETER_KINDS = frozenset({"ospf_costs"})
    LAB_NAME = "campus_lan"
    TOPO_LEVEL = "medium"
    # Distribution and server gateways bridge access ports and relay DHCP;
    # XRd Control Plane cannot route a bridged interface, so they stay FRR.
    # RouterOS routes bridged interfaces and relays DHCP, so it also runs them.
    NATIVE_PROFILE_ROLES = {
        "iosxr": frozenset({"core"}),
        "routeros": frozenset({"core", "distribution", "server_gateway"}),
    }
    NATIVE_PROFILE_SIZES = {"routeros": ("s",)}

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        routing_parameters: dict | None = None,
        device_profile: str | None = None,
        **kwargs,
    ):
        from nika.net_env.campus_lan.lab_dhcp import (
            CampusLanDhcp,
        )

        donor = CampusLanDhcp(topo_size=topo_size, **kwargs)

        self.__dict__.update(donor.__dict__)
        self.topo_size = topo_size
        from nika.net_env.synthesis.parameters import apply_routing_parameters

        apply_routing_parameters(self, routing_parameters)

        self.name = self.LAB_NAME
        if getattr(self, "lab", None) is not None:
            self.lab.name = self.LAB_NAME
        self.apply_device_profile(device_profile)

    def router_roles(self) -> dict[str, list[str]]:
        roles = {"core": [], "distribution": [], "server_gateway": []}
        for name in self.routers:
            if name.startswith("router_core_"):
                roles["core"].append(name)
            elif name.startswith("router_dist_"):
                roles["distribution"].append(name)
            else:
                roles["server_gateway"].append(name)
        return roles

    def _router_queries(self):
        from nika.net_env.campus_lan.verify import CORE_ROUTER, FRR_QUERIES

        profile = self.native_routers.get(CORE_ROUTER)
        if profile == "routeros":
            from nika.net_env.campus_lan.verify import DIST_ROUTER_DHCP
            from nika.net_env.utils.routeros.platform import routeros_campus_queries

            return routeros_campus_queries(
                FRR_QUERIES,
                gateways=self.native_routers.get(DIST_ROUTER_DHCP) == "routeros",
            )
        if profile != "iosxr":
            return FRR_QUERIES
        from dataclasses import replace

        from nika.net_env.utils.iosxr import platform

        return replace(
            FRR_QUERIES,
            ospf_process=platform.IOSXR_OSPF_PROCESS,
            ospf_running=platform.iosxr_ospf_running,
            ospf_neighbors=platform.IOSXR_OSPF_NEIGHBORS,
            ospf_full_count=platform.iosxr_ospf_full_count,
        )

    def target_roles(self) -> dict[str, list[str]]:
        hosts = list(self.hosts or [])
        pcs = [h for h in hosts if h.startswith("pc_")] or hosts
        return {
            **super().target_roles(),
            "hosts": pcs,
            "host1_pool": pcs,
            "web": pcs,
            "attacker_pool": pcs,
        }

    def business_workload(self):
        from nika.net_env.campus_lan.workload import build_workload

        return build_workload(
            list(self.hosts or []), self.frr_routers(), self.router_rate_limit_kbps()
        )

    @classmethod
    def default_probe_path(cls, *, topo_size: str = "s", **deploy_kwargs) -> ProbePath:
        return ProbePath(
            src_host="pc_1_1_1_1",
            dst_ip="10.200.0.3",
            http_url="http://web0.local/",
            http_name_url="http://web0.local/",
            control_plane_host="router_dist_1_1",
            peer_host="pc_2_1_1_1",
        )

    def startup_verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import verify_campus_lan_lab_startup

        return verify_campus_lan_lab_startup(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            queries=self._router_queries(),
        )

    def verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import verify_campus_lan_lab

        return verify_campus_lan_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            queries=self._router_queries(),
        )
