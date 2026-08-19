"""Config-scoped failure × scenario compatibility for coverage docs.

Unlike ``benchmark_full.yaml`` (one sampled config per failure × scenario),
this module answers whether a failure can actually inject on a given scenario
deploy variant (Clos/campus workload or ISP topo/igp/bgp_mode profile).
"""

from __future__ import annotations

from nika.net_env.net_env_pool import (
    CAMPUS_LAN_SCENARIO,
    DC_CLOS_SCENARIO,
    list_all_net_envs,
    scenario_tags,
)
from nika.problems.prob_pool import list_avail_problem_instances
from nika.workflows.benchmark.isp_options import ISP_SCENARIO

# Coverage-matrix columns: plain scenarios, Clos/campus workloads, ISP variants.
DC_CLOS_CONFIGS: tuple[str, ...] = ("host", "service")
CAMPUS_LAN_CONFIGS: tuple[str, ...] = ("static", "dhcp")
ISP_CONFIGS: tuple[str, ...] = ("isis", "ospf", "ibgp_rr", "abilene-ebgp")

_DC_CLOS_BASE: frozenset[str] = frozenset(
    {"arp", "link", "mac", "bgp", "icmp", "frr", "pc"}
)
_CAMPUS_STATIC: frozenset[str] = frozenset(
    {"arp", "link", "mac", "icmp", "frr", "ospf", "pc", "http", "dns"}
)
_ISP_BASE: frozenset[str] = frozenset({"isp", "sndlib", "frr", "igp", "link", "icmp"})

# Failures that need a stricter column than TAGS alone encode.
_PROBLEM_COLUMN_ALLOWLIST: dict[str, frozenset[str]] = {
    "bgp_rpki_invalid_route_leak": frozenset({f"{ISP_SCENARIO}/abilene-ebgp"}),
    "bgp_max_prefix_exceeded": frozenset({f"{ISP_SCENARIO}/abilene-ebgp"}),
    "mpls_label_limit_exceeded": frozenset({"p4_mpls"}),
    "p4_aggressive_detection_thresholds": frozenset({"p4_bloom_filter"}),
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
        if name == DC_CLOS_SCENARIO:
            columns.extend(f"{DC_CLOS_SCENARIO}/{cfg}" for cfg in DC_CLOS_CONFIGS)
        elif name == CAMPUS_LAN_SCENARIO:
            columns.extend(f"{CAMPUS_LAN_SCENARIO}/{cfg}" for cfg in CAMPUS_LAN_CONFIGS)
        elif name == ISP_SCENARIO:
            columns.extend(f"{ISP_SCENARIO}/{cfg}" for cfg in ISP_CONFIGS)
        else:
            columns.append(name)
    return columns


def effective_tags(column: str) -> frozenset[str]:
    """Tags exposed by one deployed scenario config (not class-level unions)."""
    scenario, config = parse_column(column)
    if scenario == DC_CLOS_SCENARIO:
        if config == "host":
            return _DC_CLOS_BASE
        if config == "service":
            return _DC_CLOS_BASE | frozenset({"dns", "http"})
        raise ValueError(f"Unknown dc_clos config {config!r}")
    if scenario == CAMPUS_LAN_SCENARIO:
        if config == "static":
            return _CAMPUS_STATIC
        if config == "dhcp":
            return _CAMPUS_STATIC | frozenset({"dhcp", "load_balancer", "web"})
        raise ValueError(f"Unknown campus_lan config {config!r}")
    if scenario == ISP_SCENARIO:
        if config == "isis":
            return _ISP_BASE | frozenset({"isis"})
        if config == "ospf":
            return _ISP_BASE | frozenset({"ospf"})
        if config == "ibgp_rr":
            return _ISP_BASE | frozenset({"isis", "bgp"})
        if config == "abilene-ebgp":
            return _ISP_BASE | frozenset({"isis", "bgp", "rpki"})
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
