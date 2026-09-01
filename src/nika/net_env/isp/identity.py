"""ISP scenario identity helpers (topology-baked scenario IDs)."""

from __future__ import annotations

from nika.topology.sndlib.catalog import SNDLIB_TOPOLOGY_NAMES

ISP_NAMED_SPECIALS: frozenset[str] = frozenset(
    {
        "isp_abilene_ebgp_rtbh",
        "isp_dfn-bwin_ebgp_rtbh",
        "isp_abilene_ebgp_rpki",
        "isp_geant_ebgp_rpki",
    }
)
_ISP_PREFIX = "isp_"


def isp_scenario_id(topo: str) -> str:
    """Return the registry scenario ID for an SNDlib topology name."""
    return f"{_ISP_PREFIX}{topo}"


def is_isp_named_special(scenario: str) -> bool:
    return scenario in ISP_NAMED_SPECIALS


def is_isp_base_topology(scenario: str) -> bool:
    """True for ``isp_<sndlib>`` topology scenarios (not named specials)."""
    if not scenario.startswith(_ISP_PREFIX) or scenario in ISP_NAMED_SPECIALS:
        return False
    return scenario[len(_ISP_PREFIX) :] in SNDLIB_TOPOLOGY_NAMES


def is_isp_scenario(scenario: str) -> bool:
    """True for any flattened ISP scenario (base topo or named special)."""
    return is_isp_base_topology(scenario) or is_isp_named_special(scenario)


def isp_topo_from_scenario(scenario: str) -> str:
    """Resolve the SNDlib topology name baked into an ISP scenario ID."""
    if scenario == "isp_abilene_ebgp_rtbh":
        return "abilene"
    if scenario == "isp_dfn-bwin_ebgp_rtbh":
        return "dfn-bwin"
    if scenario == "isp_abilene_ebgp_rpki":
        return "abilene"
    if scenario == "isp_geant_ebgp_rpki":
        return "geant"
    if is_isp_base_topology(scenario):
        return scenario[len(_ISP_PREFIX) :]
    raise ValueError(f"Not an ISP scenario: {scenario!r}")


def list_isp_base_scenarios() -> tuple[str, ...]:
    return tuple(isp_scenario_id(name) for name in SNDLIB_TOPOLOGY_NAMES)
