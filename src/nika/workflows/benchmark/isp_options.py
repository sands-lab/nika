"""ISP deploy options for benchmark cases."""

from __future__ import annotations

from typing import Any

from nika.net_env.isp.identity import (
    ISP_NAMED_SPECIALS,
    is_isp_base_topology,
    is_isp_named_special,
    is_isp_scenario,
    isp_scenario_id,
    isp_topo_from_scenario,
    list_isp_base_scenarios,
)
from nika.net_env.isp.bgp.config import (
    DEFAULT_BGP_MODE,
    ISP_BGP_MODES,
    normalize_bgp_mode,
)
from nika.net_env.isp.bgp.errors import BgpConfigError
from nika.net_env.isp.igp.config import DEFAULT_IGP, SUPPORTED_IGPS
from nika.net_env.isp.profiles import (
    DEFAULT_BACKEND_FOR_ISP,
    default_device_profile,
    normalize_device_profile,
    validate_backend_profile,
)
from nika.topology.sndlib.catalog import topology_size_for_name

ISP_OPTION_KEYS = ("igp", "bgp_mode", "rpki", "backend", "device_profile")

__all__ = [
    "ISP_NAMED_SPECIALS",
    "ISP_OPTION_KEYS",
    "is_isp_base_topology",
    "is_isp_named_special",
    "is_isp_scenario",
    "isp_scenario_id",
    "isp_topo_from_scenario",
    "list_isp_base_scenarios",
    "isp_config_for_problem",
    "isp_column_suffix",
    "isp_options_from_row",
    "validate_and_resolve_isp_options",
    "isp_stack_for_backend",
]


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


def isp_stack_for_backend(backend: str) -> dict[str, str]:
    """Return the only supported (backend, device_profile) pair for ``backend``."""
    profile = default_device_profile(backend)
    validate_backend_profile(backend, profile)
    return {"backend": backend, "device_profile": profile}


def isp_config_for_problem(problem: str, problem_tags: set[str]) -> dict[str, Any]:
    """Pick ISP protocol options from failure needs (topology comes from scenario)."""
    if problem == "bgp_rpki_invalid_route_leak":
        return {"igp": "ospf", "bgp_mode": "ebgp", "rpki": True}
    if problem == "bgp_max_prefix_exceeded":
        return {"igp": "ospf", "bgp_mode": "ebgp", "rpki": False}
    if "ospf" in problem_tags or problem.startswith("ospf_"):
        return {"igp": "ospf", "bgp_mode": "none", "rpki": False}
    if "bgp" in problem_tags or problem.startswith("bgp_"):
        return {"igp": DEFAULT_IGP, "bgp_mode": "ibgp_rr", "rpki": False}
    return {"igp": DEFAULT_IGP, "bgp_mode": DEFAULT_BGP_MODE, "rpki": False}


def isp_column_suffix(
    *,
    topo: str | None = None,
    igp: str | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
) -> str:
    """Legacy matrix column suffix helper (prefer scenario IDs in new code)."""
    resolved_topo = topo or "polska"
    resolved_igp = igp or DEFAULT_IGP
    resolved_bgp = bgp_mode or DEFAULT_BGP_MODE
    resolved_rpki = bool(rpki)
    if resolved_rpki:
        return f"{resolved_topo}-{resolved_bgp}-rpki"
    if resolved_topo != "polska":
        return f"{resolved_topo}-{resolved_bgp}"
    if resolved_bgp != "none":
        return resolved_bgp
    return resolved_igp


def _resolve_stack(
    *,
    scenario: str,
    backend: Any,
    device_profile: Any,
) -> dict[str, str]:
    raw_backend = None if backend in (None, "", "-") else str(backend).strip().lower()
    raw_profile = (
        None
        if device_profile in (None, "", "-")
        else str(device_profile).strip().lower()
    )
    if is_isp_named_special(scenario):
        if raw_backend not in (None, DEFAULT_BACKEND_FOR_ISP):
            raise ValueError(
                f"Scenario {scenario!r} is Kathara-only; got backend={raw_backend!r}."
            )
        if raw_profile not in (None, "frr"):
            raise ValueError(
                f"Scenario {scenario!r} uses device_profile=frr; got {raw_profile!r}."
            )
        return isp_stack_for_backend(DEFAULT_BACKEND_FOR_ISP)

    resolved_backend = raw_backend or DEFAULT_BACKEND_FOR_ISP
    if raw_profile is None:
        resolved_profile = default_device_profile(resolved_backend)
    else:
        resolved_profile = normalize_device_profile(raw_profile)
    validate_backend_profile(resolved_backend, resolved_profile)
    return {"backend": resolved_backend, "device_profile": resolved_profile}


def isp_options_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    """Return ISP kwargs from a normalized case row, or ``None`` when not ISP."""
    scenario = str(row.get("scenario") or "")
    if not is_isp_scenario(scenario):
        return None
    stack = _resolve_stack(
        scenario=scenario,
        backend=row.get("backend"),
        device_profile=row.get("device_profile"),
    )
    if is_isp_named_special(scenario):
        return dict(stack)
    return {
        "igp": str(row.get("igp") or DEFAULT_IGP),
        "bgp_mode": str(row.get("bgp_mode") or DEFAULT_BGP_MODE),
        "rpki": bool(row.get("rpki", False)),
        **stack,
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
    backend: Any = None,
    device_profile: Any = None,
) -> dict[str, Any] | None:
    """Validate ISP option fields; return resolved kwargs or ``None`` for non-ISP.

    Topology identity is the scenario ID. Base ``isp_<topo>`` accepts protocol
    options; named specials reject overrides. ``topo`` must not be supplied.
    """
    provided = {
        "topo": None if topo in (None, "", "-") else str(topo),
        "igp": None if igp in (None, "", "-") else str(igp),
        "bgp_mode": None if bgp_mode in (None, "", "-") else str(bgp_mode),
        "rpki": _parse_rpki(rpki),
        "backend": None if backend in (None, "", "-") else str(backend),
        "device_profile": (
            None if device_profile in (None, "", "-") else str(device_profile)
        ),
    }
    any_protocol = any(
        provided[key] is not None for key in ("igp", "bgp_mode", "rpki")
    )
    any_stack = any(
        provided[key] is not None for key in ("backend", "device_profile")
    )
    topo_provided = provided["topo"] is not None

    if not is_isp_scenario(scenario):
        if any_protocol or topo_provided or any_stack:
            raise ValueError(
                f"Scenario {scenario!r} does not accept topo/igp/bgp_mode/rpki/"
                f"backend/device_profile fields "
                f"(got topo={provided['topo']!r}, igp={provided['igp']!r}, "
                f"bgp_mode={provided['bgp_mode']!r}, rpki={provided['rpki']!r}, "
                f"backend={provided['backend']!r}, "
                f"device_profile={provided['device_profile']!r})."
            )
        return None

    if topo_provided:
        raise ValueError(
            f"Scenario {scenario!r} bakes topology into the scenario name; "
            f"omit topo (got {provided['topo']!r})."
        )

    baked_topo = isp_topo_from_scenario(scenario)
    expected_size = topology_size_for_name(baked_topo)
    if topo_size and topo_size != expected_size:
        raise ValueError(
            f"Scenario {scenario!r} has fixed topo_size {expected_size!r}; "
            f"got {topo_size!r}."
        )

    stack = _resolve_stack(
        scenario=scenario,
        backend=provided["backend"],
        device_profile=provided["device_profile"],
    )

    if is_isp_named_special(scenario):
        if any_protocol:
            raise ValueError(
                f"Scenario {scenario!r} uses a fixed protocol profile; omit "
                f"igp/bgp_mode/rpki (got igp={provided['igp']!r}, "
                f"bgp_mode={provided['bgp_mode']!r}, rpki={provided['rpki']!r})."
            )
        return dict(stack)

    tags = problem_tags if problem_tags is not None else set()
    defaults = isp_config_for_problem(problem, tags)
    if defaults.get("rpki"):
        raise ValueError(
            f"Problem {problem!r} requires a named RPKI scenario "
            f"(isp_abilene_ebgp_rpki or isp_geant_ebgp_rpki), not {scenario!r}."
        )

    resolved = {
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
        **stack,
    }
    if resolved["rpki"]:
        raise ValueError(
            f"rpki=true is only valid on named RPKI scenarios; use "
            f"isp_abilene_ebgp_rpki or isp_geant_ebgp_rpki (not {scenario!r})."
        )
    if resolved["igp"] not in SUPPORTED_IGPS:
        raise ValueError(
            f"Invalid igp {resolved['igp']!r} for scenario {scenario!r}; "
            f"expected one of {SUPPORTED_IGPS}."
        )
    try:
        resolved["bgp_mode"] = normalize_bgp_mode(resolved["bgp_mode"])
    except BgpConfigError as exc:
        raise ValueError(str(exc)) from exc
    if resolved["bgp_mode"] not in ISP_BGP_MODES:
        raise ValueError(
            f"Invalid bgp_mode {resolved['bgp_mode']!r} for scenario "
            f"{scenario!r}; expected one of {ISP_BGP_MODES}."
        )
    return resolved
