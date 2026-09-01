"""Named ISP specials that bake topology + protocol overlays."""

from __future__ import annotations

from nika.net_env.isp.kathara.lab import Isp


class IspAbileneEbgpRpki(Isp):
    """Abilene eBGP lab with offline RPKI/ROV (Routinator)."""

    LAB_NAME = "isp_abilene_ebgp_rpki"
    TOPO_SIZE = "s"
    TAGS = [
        "isp",
        "sndlib",
        "frr",
        "ospf",
        "bgp",
        "ebgp",
        "rpki",
        "igp",
        "link",
        "icmp",
    ]

    def __init__(self, **kwargs) -> None:
        kwargs.pop("topo", None)
        kwargs.pop("topo_size", None)
        kwargs.pop("igp", None)
        kwargs.pop("bgp_mode", None)
        kwargs.pop("rpki", None)
        kwargs.pop("rtbh", None)
        kwargs.pop("size", None)
        kwargs.pop("scenario_id", None)
        super().__init__(
            topo="abilene",
            igp="ospf",
            bgp_mode="ebgp",
            rpki=True,
            rtbh=False,
            scenario_id=self.LAB_NAME,
            **kwargs,
        )
        self.name = self.LAB_NAME
        self.desc = (
            "Abilene ISP topology with OSPF inside each AS, eBGP between "
            "three AS regions, and offline RPKI/ROV (FRR)."
        )


class IspGeantEbgpRpki(Isp):
    """GEANT eBGP lab with offline RPKI/ROV (Routinator)."""

    LAB_NAME = "isp_geant_ebgp_rpki"
    TOPO_SIZE = "m"
    TAGS = [
        "isp",
        "sndlib",
        "frr",
        "ospf",
        "bgp",
        "ebgp",
        "rpki",
        "igp",
        "link",
        "icmp",
    ]

    def __init__(self, **kwargs) -> None:
        kwargs.pop("topo", None)
        kwargs.pop("topo_size", None)
        kwargs.pop("igp", None)
        kwargs.pop("bgp_mode", None)
        kwargs.pop("rpki", None)
        kwargs.pop("rtbh", None)
        kwargs.pop("size", None)
        kwargs.pop("scenario_id", None)
        super().__init__(
            topo="geant",
            igp="ospf",
            bgp_mode="ebgp",
            rpki=True,
            rtbh=False,
            scenario_id=self.LAB_NAME,
            **kwargs,
        )
        self.name = self.LAB_NAME
        self.desc = (
            "GEANT ISP topology with OSPF inside each AS, eBGP between "
            "three AS regions, and offline RPKI/ROV (FRR)."
        )
