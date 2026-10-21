"""Campus LAN fabric with DHCP, DNS, and load-balanced HTTP services."""

from __future__ import annotations

from typing import Literal

from nika.net_env.base import NetworkEnvBase, ProbePath


class CampusLan(NetworkEnvBase):
    ROUTING_PARAMETER_KINDS = frozenset({"ospf_costs"})
    LAB_NAME = "campus_lan"
    TOPO_LEVEL = "medium"

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        routing_parameters: dict | None = None,
        **kwargs,
    ):
        from nika.net_env.campus_lan.lab_dhcp import (
            CampusLanDhcp,
        )

        donor = CampusLanDhcp(topo_size=topo_size, **kwargs)

        self.__dict__.update(donor.__dict__)
        from nika.net_env.synthesis.parameters import apply_routing_parameters

        apply_routing_parameters(self, routing_parameters)

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

    def _verify(self, verify) -> dict:
        runtime = self._build_runtime()
        if self.device_profile == "frr":
            return verify(runtime, scenario_name=self.LAB_NAME)
        from nika.net_env.campus_lan.verify import routeros_queries
        from nika.net_env.utils.routeros.common import with_routeros_config_check

        result = verify(
            runtime, scenario_name=self.LAB_NAME, queries=routeros_queries()
        )
        routers = [
            name
            for name, identity in self.machine_identities.items()
            if "routeros" in identity.capabilities
        ]
        return with_routeros_config_check(runtime, routers, result)

    def startup_verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import verify_campus_lan_lab_startup

        return self._verify(verify_campus_lan_lab_startup)

    def verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import verify_campus_lan_lab

        return self._verify(verify_campus_lan_lab)

    def deploy(self):
        if self.device_profile == "routeros":
            from nika.net_env.utils.routeros.common import require_routeros_image

            require_routeros_image()
        super().deploy()
