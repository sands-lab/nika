"""ISP deploy options for benchmark cases."""

from __future__ import annotations

from typing import Any

from nika.net_env.isp.bgp.config import (
    DEFAULT_BGP_MODE,
    ISP_BGP_MODES,
    normalize_bgp_mode,
)
from nika.net_env.isp.bgp.errors import BgpConfigError
from nika.net_env.isp.igp.config import DEFAULT_IGP, DEFAULT_TOPO, SUPPORTED_IGPS
from nika.topology.sndlib.catalog import topology_for_size

ISP_SCENARIO = "isp"
ISP_OPTION_KEYS = ("topo", "igp", "bgp_mode", "rpki")


def _parse_rpki(value: Any) -> bool | None:
    if value in (None, "", "-"):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Invalid rpki value {value!r}; expected true/false.")


def isp_config_for_problem(problem: str, problem_tags: set[str]) -> dict[str, Any]:
    """Pick ISP deploy options from failure needs (not a cartesian product)."""
    if problem == "bgp_rpki_invalid_route_leak":
        return {
            "topo": "abilene",
            "igp": "ospf",
            "bgp_mode": "ebgp",
            "rpki": True,
        }
    if problem == "bgp_max_prefix_exceeded":
        return {"topo": "abilene", "igp": "isis", "bgp_mode": "ebgp", "rpki": False}
    if "ospf" in problem_tags or problem.startswith("ospf_"):
        return {
            "topo": DEFAULT_TOPO,
            "igp": "ospf",
            "bgp_mode": "none",
            "rpki": False,
        }
    if "bgp" in problem_tags or problem.startswith("bgp_"):
        return {
            "topo": DEFAULT_TOPO,
            "igp": DEFAULT_IGP,
            "bgp_mode": "ibgp_rr",
            "rpki": False,
        }
    return {
        "topo": DEFAULT_TOPO,
        "igp": DEFAULT_IGP,
        "bgp_mode": DEFAULT_BGP_MODE,
        "rpki": False,
    }


def isp_column_suffix(
    *,
    topo: str | None = None,
    igp: str | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
) -> str:
    """Matrix column suffix for an ISP case (``isp/{suffix}``)."""
    resolved_topo = topo or DEFAULT_TOPO
    resolved_igp = igp or DEFAULT_IGP
    resolved_bgp = bgp_mode or DEFAULT_BGP_MODE
    resolved_rpki = bool(rpki)
    if resolved_rpki:
        return f"{resolved_topo}-{resolved_bgp}-rpki"
    if resolved_topo != DEFAULT_TOPO:
        return f"{resolved_topo}-{resolved_bgp}"
    if resolved_bgp != "none":
        return resolved_bgp
    return resolved_igp


def isp_options_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return ISP kwargs from a normalized case row, or ``None`` when not ISP."""
    if str(row.get("scenario")) != ISP_SCENARIO:
        return None
    return {
        "topo": str(row.get("topo") or DEFAULT_TOPO),
        "igp": str(row.get("igp") or DEFAULT_IGP),
        "bgp_mode": str(row.get("bgp_mode") or DEFAULT_BGP_MODE),
        "rpki": bool(row.get("rpki", False)),
    }


def validate_and_resolve_isp_options(
    *,
    scenario: str,
    problem: str,
    problem_tags: set[str] | None = None,
    topo_size: str = "",
    topo: Any = None,
    igp: Any = None,
    bgp_mode: Any = None,
    rpki: Any = None,
) -> dict[str, Any] | None:
    """Validate ISP option fields; return resolved kwargs or ``None`` for non-ISP.

    Missing fields on ``isp`` are filled from :func:`isp_config_for_problem`.
    Non-ISP scenarios reject any of ``topo`` / ``igp`` / ``bgp_mode`` / ``rpki``.
    """
    provided = {
        "topo": None if topo in (None, "", "-") else str(topo),
        "igp": None if igp in (None, "", "-") else str(igp),
        "bgp_mode": None if bgp_mode in (None, "", "-") else str(bgp_mode),
        "rpki": _parse_rpki(rpki),
    }
    any_provided = any(value is not None for value in provided.values())
    if scenario != ISP_SCENARIO:
        if any_provided:
            raise ValueError(
                f"Scenario {scenario!r} does not accept topo/igp/bgp_mode/rpki "
                f"fields (got topo={provided['topo']!r}, igp={provided['igp']!r}, "
                f"bgp_mode={provided['bgp_mode']!r}, rpki={provided['rpki']!r})."
            )
        return None

    tags = problem_tags if problem_tags is not None else set()
    defaults = isp_config_for_problem(problem, tags)
    default_topo = defaults["topo"]
    if problem not in {"bgp_max_prefix_exceeded", "bgp_rpki_invalid_route_leak"}:
        default_topo = topology_for_size(topo_size or "s")
    resolved = {
        "topo": provided["topo"] if provided["topo"] is not None else default_topo,
        "igp": provided["igp"] if provided["igp"] is not None else defaults["igp"],
        "bgp_mode": (
            provided["bgp_mode"]
            if provided["bgp_mode"] is not None
            else str(defaults["bgp_mode"])
        ),
        "rpki": bool(
            provided["rpki"]
            if provided["rpki"] is not None
            else defaults.get("rpki", False)
        ),
    }
    if resolved["igp"] not in SUPPORTED_IGPS:
        raise ValueError(
            f"Invalid igp {resolved['igp']!r} for scenario {ISP_SCENARIO!r}; "
            f"expected one of {SUPPORTED_IGPS}."
        )
    try:
        resolved["bgp_mode"] = normalize_bgp_mode(resolved["bgp_mode"])
    except BgpConfigError as exc:
        raise ValueError(str(exc)) from exc
    if resolved["bgp_mode"] not in ISP_BGP_MODES:
        raise ValueError(
            f"Invalid bgp_mode {resolved['bgp_mode']!r} for scenario "
            f"{ISP_SCENARIO!r}; expected one of {ISP_BGP_MODES}."
        )
    if resolved["rpki"] and resolved["bgp_mode"] != "ebgp":
        raise ValueError(
            f"rpki=true requires bgp_mode 'ebgp' (got {resolved['bgp_mode']!r})."
        )
    return resolved
