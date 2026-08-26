"""Failure × scenario compatibility for coverage docs."""

from __future__ import annotations

from nika.net_env.net_env_pool import list_all_net_envs, scenario_tags
from nika.problems.prob_pool import list_avail_problem_instances
from nika.workflows.benchmark.isp_options import ISP_SCENARIO

ISP_CONFIGS: tuple[str, ...] = (
    "isis",
    "ospf",
    "ibgp_rr",
    "abilene-ebgp",
    "abilene-ebgp-rpki",
    "geant-ebgp-rpki",
)

_ISP_BASE: frozenset[str] = frozenset({"isp", "sndlib", "frr", "igp", "link", "icmp"})

# Failures that need a stricter column than TAGS alone encode.
_PROBLEM_COLUMN_ALLOWLIST: dict[str, frozenset[str]] = {
    "p4_tcam_entry_corruption": frozenset({"p4_dc_gateway"}),
    "silent_egress_packet_loss": frozenset({"p4_dc_gateway"}),
    "p4_ecn_threshold_misconfiguration": frozenset({"p4_dc_gateway"}),
    "tcp_syn_flood_attack": frozenset({"p4_dc_gateway"}),
    "int_insufficient_mtu_headroom": frozenset({"p4_dc_gateway"}),
    "bgp_rpki_invalid_route_leak": frozenset(
        {
            f"{ISP_SCENARIO}/abilene-ebgp-rpki",
            f"{ISP_SCENARIO}/geant-ebgp-rpki",
        }
    ),
    "bgp_max_prefix_exceeded": frozenset({f"{ISP_SCENARIO}/abilene-ebgp"}),
}


def parse_column(column: str) -> tuple[str, str | None]:
    """Return ``(scenario, config)`` for a coverage column id."""
    if "/" in column:
        scenario, _, config = column.partition("/")
        return scenario, config
    return column, None


def coverage_columns() -> list[str]:
    """Stable ordered list of coverage-matrix column ids."""
    columns: list[str] = []
    for name in sorted(list_all_net_envs()):
        if name == ISP_SCENARIO:
            columns.extend(f"{ISP_SCENARIO}/{cfg}" for cfg in ISP_CONFIGS)
        else:
            columns.append(name)
    return columns


def effective_tags(column: str) -> frozenset[str]:
    """Tags exposed by one deployed scenario config (not class-level unions)."""
    scenario, config = parse_column(column)
    if scenario == ISP_SCENARIO:
        if config == "isis":
            return _ISP_BASE | frozenset({"isis"})
        if config == "ospf":
            return _ISP_BASE | frozenset({"ospf"})
        if config == "ibgp_rr":
            return _ISP_BASE | frozenset({"isis", "bgp"})
        if config == "abilene-ebgp":
            return _ISP_BASE | frozenset({"isis", "bgp"})
        if config == "abilene-ebgp-rpki":
            return _ISP_BASE | frozenset({"ospf", "bgp", "rpki"})
        if config == "geant-ebgp-rpki":
            return _ISP_BASE | frozenset({"ospf", "bgp", "rpki"})
        raise ValueError(f"Unknown isp config {config!r}")
    return frozenset(scenario_tags(scenario))


def problem_tags(problem: str) -> frozenset[str]:
    cls = list_avail_problem_instances().get(problem)
    if cls is None:
        raise ValueError(f"Unknown problem {problem!r}")
    return frozenset(cls.TAGS)


def compatible(problem: str, column: str) -> bool:
    """Return whether ``problem`` can inject usefully on ``column``."""
    allow = _PROBLEM_COLUMN_ALLOWLIST.get(problem)
    if allow is not None and column not in allow:
        return False
    return problem_tags(problem).issubset(effective_tags(column))


def compatible_columns(problem: str) -> list[str]:
    """Return coverage columns compatible with ``problem``."""
    return [col for col in coverage_columns() if compatible(problem, col)]
