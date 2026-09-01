"""Named ISP specials that bake topology + RTBH policy overlays."""

from __future__ import annotations

from nika.net_env.isp.kathara.lab import Isp


class _IspEbgpRtbh(Isp):
    TOPOLOGY: str
    DESCRIPTION_NAME: str
    TAGS = [
        "isp",
        "sndlib",
        "frr",
        "ospf",
        "bgp",
        "ebgp",
        "rtbh",
        "igp",
        "link",
        "icmp",
    ]

    def __init__(self, **kwargs) -> None:
        for key in (
            "topo",
            "topo_size",
            "igp",
            "bgp_mode",
            "rpki",
            "rtbh",
            "size",
            "scenario_id",
        ):
            kwargs.pop(key, None)
        super().__init__(
            topo=self.TOPOLOGY,
            igp="ospf",
            bgp_mode="ebgp",
            rtbh=True,
            rpki=False,
            scenario_id=self.LAB_NAME,
            **kwargs,
        )
        self.name = self.LAB_NAME
        self.desc = (
            f"{self.DESCRIPTION_NAME} ISP topology with OSPF inside each AS, "
            "eBGP between three AS regions, and an RTBH community policy (FRR)."
        )


class IspAbileneEbgpRtbh(_IspEbgpRtbh):
    """Abilene eBGP lab with an RTBH community blackhole profile."""

    LAB_NAME = "isp_abilene_ebgp_rtbh"
    TOPO_SIZE = "s"
    TOPOLOGY = "abilene"
    DESCRIPTION_NAME = "Abilene"


class IspDfnBwinEbgpRtbh(_IspEbgpRtbh):
    """DFN-BWIN eBGP lab with an RTBH community blackhole profile."""

    LAB_NAME = "isp_dfn-bwin_ebgp_rtbh"
    TOPO_SIZE = "s"
    TOPOLOGY = "dfn-bwin"
    DESCRIPTION_NAME = "DFN-BWIN"
