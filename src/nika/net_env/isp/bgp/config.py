"""ISP BGP mode configuration."""

from __future__ import annotations

from typing import Literal

from nika.net_env.isp.bgp.errors import BgpConfigError

IspBgpMode = Literal["none", "ibgp_rr", "ebgp"]

SUPPORTED_BGP_MODES: tuple[IspBgpMode, ...] = ("none", "ibgp_rr", "ebgp")
DEFAULT_BGP_MODE: IspBgpMode = "none"

# Preset constants (NIKA policy — not derived from SNDlib).
IBGP_ASN = 65000
EBGP_BASE_ASN = 65001
IBGP_BUSINESS_POOL = "203.0.113.0/24"  # TEST-NET-3
EBGP_BUSINESS_POOL = "198.51.100.0/24"  # TEST-NET-2


def normalize_bgp_mode(raw: str | None) -> IspBgpMode:
    mode: IspBgpMode = DEFAULT_BGP_MODE if raw is None else raw  # type: ignore[assignment]
    if mode not in SUPPORTED_BGP_MODES:
        raise BgpConfigError(
            f"Unsupported bgp_mode {raw!r}; expected one of {SUPPORTED_BGP_MODES}."
        )
    return mode
