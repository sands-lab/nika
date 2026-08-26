"""Campus LAN fabric with DHCP, DNS, and load-balanced HTTP services."""

from __future__ import annotations

from typing import Literal

from nika.net_env.base import NetworkEnvBase


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
        "forwarding_device",
        "web",
    ]

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

    def verify_lab(self) -> dict:
        from nika.net_env.campus_lan.verify import (
            verify_campus_lan_lab,
        )

        return verify_campus_lan_lab(
            self._build_runtime(),
            scenario_name=self.LAB_NAME,
        )
