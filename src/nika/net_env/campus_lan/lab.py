"""Campus LAN fabric with DHCP, DNS, and load-balanced HTTP services."""

from __future__ import annotations

from typing import Literal

from nika.net_env.base import NetworkEnvBase, ProbePath


class CampusLan(NetworkEnvBase):
    LAB_NAME = "campus_lan"
    TOPO_LEVEL = "medium"

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        **kwargs,
    ):
        from nika.net_env.campus_lan.lab_dhcp import (
            CampusLanDhcp,
        )

        donor = CampusLanDhcp(topo_size=topo_size, **kwargs)

        self.__dict__.update(donor.__dict__)
        self.name = self.LAB_NAME
        if getattr(self, "lab", None) is not None:
            self.lab.name = self.LAB_NAME

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
        )

    def verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import verify_campus_lan_lab

        return verify_campus_lan_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
        )
