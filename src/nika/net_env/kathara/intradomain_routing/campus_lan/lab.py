"""Unified campus LAN fabric with static or dhcp host workload."""

from __future__ import annotations

from typing import Literal

from nika.net_env.base import NetworkEnvBase

Workload = Literal["static", "dhcp"]


class CampusLan(NetworkEnvBase):
    LAB_NAME = "campus_lan"
    TOPO_LEVEL = "medium"
    TOPO_SIZE = ["s", "m", "l"]
    TAGS = [
        "arp",
        "link",
        "mac",
        "icmp",
        "frr",
        "ospf",
        "pc",
        "http",
        "dns",
        "dhcp",
        "load_balancer",
        "web",
    ]

    def __init__(
        self,
        topo_size: Literal["s", "m", "l"] = "s",
        workload: Workload = "static",
        **kwargs,
    ):
        if workload not in ("static", "dhcp"):
            raise ValueError("workload must be 'static' or 'dhcp'.")
        if workload == "dhcp":
            from nika.net_env.kathara.intradomain_routing.campus_lan.lab_dhcp import (
                OSPFEnterpriseDHCP,
            )

            donor = OSPFEnterpriseDHCP(topo_size=topo_size, **kwargs)
        else:
            from nika.net_env.kathara.intradomain_routing.campus_lan.lab_static import (
                OSPFEnterpriseStatic,
            )

            donor = OSPFEnterpriseStatic(topo_size=topo_size, **kwargs)

        self.__dict__.update(donor.__dict__)
        self.workload: Workload = workload
        self.name = self.LAB_NAME
        if getattr(self, "lab", None) is not None:
            self.lab.name = self.LAB_NAME

    def verify_lab(self) -> dict:
        from nika.net_env.kathara.intradomain_routing.campus_lan.verify import (
            verify_campus_lan_lab,
        )

        return verify_campus_lan_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
            workload=self.workload,
        )
