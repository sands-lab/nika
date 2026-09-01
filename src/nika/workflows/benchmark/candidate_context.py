"""Shared candidate-pool helpers for audit and coverage-guided selection."""

from __future__ import annotations

from typing import Any

from nika.problems.registry import list_avail_problem_instances
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.isp_options import is_isp_base_topology
from nika.workflows.benchmark.resume import benchmark_option_id

MAJOR_SCENARIOS: tuple[str, ...] = (
    "dc_clos",
    "campus_lan",
    "enterprise_branch",
    "isp_abilene",
    "isp_france",
    "isp_pioro40",
    "isp_abilene_ebgp_rtbh",
    "isp_abilene_ebgp_rpki",
    "sdn_l3_clos",
    "p4_dc_fabric",
    "p4_dc_gateway",
    "k8s_lab",
    "llmd_lab",
    "min3clos",
)

SCALABLE_SCENARIOS: frozenset[str] = frozenset(
    {
        "dc_clos",
        "campus_lan",
        "enterprise_branch",
        "sdn_l3_clos",
        "p4_dc_fabric",
        "p4_dc_gateway",
    }
)


def normalize_topo_scale(row: dict[str, Any]) -> str:
    """Return ``s`` / ``m`` / ``l`` or ``fixed`` for fixed-size scenarios."""
    scale = str(row.get("topo_size") or "").strip()
    if scale in {"s", "m", "l"}:
        return scale
    return "fixed"


def isp_profile_key(row: dict[str, Any]) -> tuple[str, str, bool]:
    return (
        str(row.get("igp") or ""),
        str(row.get("bgp_mode") or ""),
        bool(row.get("rpki", False)),
    )


def stack_profile_key(row: dict[str, Any]) -> tuple[str, str]:
    """Backend + device-stack identity (empty when not declared on the row)."""
    return (
        str(row.get("backend") or ""),
        str(row.get("device_profile") or ""),
    )


def deployment_environment_key(
    row: dict[str, Any],
) -> tuple[str, str, str, str, bool, str, str]:
    """Identity of one benchmark deployment environment (includes ISP stack)."""
    return (
        str(row.get("scenario") or ""),
        normalize_topo_scale(row),
        *isp_profile_key(row),
        *stack_profile_key(row),
    )


def pool_context_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Collapse key: one representative inject per deployment context."""
    scenario = str(row.get("scenario") or "")
    if is_healthy_case(row.get("problem")):
        if is_isp_base_topology(scenario):
            return (
                "healthy",
                scenario,
                normalize_topo_scale(row),
                *isp_profile_key(row),
                *stack_profile_key(row),
            )
        return ("healthy", scenario, normalize_topo_scale(row), *stack_profile_key(row))
    if is_isp_base_topology(scenario):
        return (
            row["problem"],
            scenario,
            normalize_topo_scale(row),
            *isp_profile_key(row),
            *stack_profile_key(row),
        )
    return (row["problem"], scenario, normalize_topo_scale(row), *stack_profile_key(row))


def selection_context_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Distinct root-cause and deployment-environment identity."""
    return (str(row["problem"]), *deployment_environment_key(row))


def failure_domain_for(problem: str) -> str:
    if is_healthy_case(problem):
        return ""
    cls = list_avail_problem_instances().get(problem)
    if cls is None:
        return ""
    domain = cls.failure_domain
    return str(domain.value if hasattr(domain, "value") else domain)


def enrich_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """Attach selection metadata without mutating the source row."""
    problem = str(row["problem"])
    enriched = dict(row)
    enriched["is_healthy"] = is_healthy_case(problem)
    enriched["failure_domain"] = failure_domain_for(problem)
    enriched["topo_scale"] = normalize_topo_scale(row)
    enriched["deployment_environment_key"] = deployment_environment_key(row)
    enriched["pool_context_key"] = pool_context_key(row)
    enriched["selection_context_key"] = selection_context_key(row)
    return enriched


def collapse_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one canonical row per pool context (lowest option id)."""
    buckets: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        enriched = enrich_candidate(row)
        key = enriched["pool_context_key"]
        option_id = str(
            enriched.get("candidate_option_id") or benchmark_option_id(enriched)
        )
        enriched["candidate_option_id"] = option_id
        current = buckets.get(key)
        if current is None or option_id < str(current["candidate_option_id"]):
            buckets[key] = enriched
    return [buckets[key] for key in sorted(buckets)]


def row_to_case(row: dict[str, Any]) -> dict[str, Any]:
    """Export a flat benchmark case (strip loader-only fields)."""
    case: dict[str, Any] = {
        "scenario": row["scenario"],
        "problem": row["problem"],
        "inject": dict(row.get("inject") or {}),
    }
    topo_size = row.get("topo_size") or ""
    if topo_size:
        case["topo_size"] = topo_size
    for key in ("igp", "bgp_mode", "rpki", "backend", "device_profile"):
        if key in row:
            case[key] = row[key]
    root_causes = row.get("root_causes")
    if root_causes is not None:
        case["root_causes"] = root_causes
    return case
