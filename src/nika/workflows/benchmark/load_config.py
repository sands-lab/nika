"""Load benchmark case definitions from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from nika.net_env.net_env_pool import (
    DC_CLOS_SCENARIO,
    CAMPUS_LAN_SCENARIO,
    is_dc_clos_scenario,
    is_campus_lan_scenario,
    resolve_scenario_ref,
)


def normalize_benchmark_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize scenario aliases and ``workload`` on one case mapping."""
    raw_scenario = str(row["scenario"])
    canonical, alias_workload = resolve_scenario_ref(raw_scenario)
    workload = row.get("workload")
    if workload in ("-", "", None):
        workload = None
    elif workload is not None:
        workload = str(workload)

    if is_dc_clos_scenario(canonical):
        if workload is None:
            workload = alias_workload or "host"
        if workload not in ("host", "service"):
            raise ValueError(
                f"Invalid workload {workload!r} for scenario {canonical!r}; "
                "expected 'host' or 'service'."
            )
    elif is_campus_lan_scenario(canonical):
        if workload is None:
            workload = alias_workload or "static"
        if workload not in ("static", "dhcp"):
            raise ValueError(
                f"Invalid workload {workload!r} for scenario {canonical!r}; "
                "expected 'static' or 'dhcp'."
            )
    elif workload is not None:
        raise ValueError(
            f"Scenario {canonical!r} does not accept a workload field "
            f"(got {workload!r})."
        )

    topo = row.get("topo_size")
    if topo is None:
        topo = ""
    inject = row.get("inject") or {}
    if not isinstance(inject, dict):
        raise ValueError("'inject' must be a mapping")
    if not inject:
        raise ValueError(
            f"Case {canonical}/{row.get('problem')} missing non-empty 'inject' map"
        )

    normalized: dict[str, Any] = {
        "scenario": canonical,
        "problem": str(row["problem"]),
        "topo_size": "" if topo in ("-", "", None) else str(topo),
        "inject": {str(k): str(v) for k, v in inject.items()},
    }
    if canonical in (DC_CLOS_SCENARIO, CAMPUS_LAN_SCENARIO):
        normalized["workload"] = workload
    if row.get("root_causes"):
        normalized["root_causes"] = row["root_causes"]
    if row.get("root_causes_status"):
        normalized["root_causes_status"] = row["root_causes_status"]
    return normalized


def load_benchmark_yaml(path: str | Path) -> list[dict[str, Any]]:
    """Load benchmark cases from a YAML file.

    Expected shape::

        cases:
          - scenario: simple_bgp
            topo_size: null
            problem: link_down
            inject:
              host_name: pc1
              intf_name: eth0

    Legacy Clos / campus LAN scenario ids are rewritten to the unified
    scenario with the matching ``workload``.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "cases" not in data:
        raise ValueError(f"Invalid benchmark YAML (missing top-level 'cases'): {path}")
    cases = data["cases"]
    if not isinstance(cases, list):
        raise ValueError(f"Invalid benchmark YAML ('cases' must be a list): {path}")
    normalized: list[dict[str, Any]] = []
    for idx, row in enumerate(cases):
        if not isinstance(row, dict):
            raise ValueError(f"Benchmark case {idx} must be a mapping")
        required = ("scenario", "problem")
        for key in required:
            if key not in row:
                raise ValueError(f"Benchmark case {idx} missing required field {key!r}")
        try:
            normalized.append(normalize_benchmark_row(row))
        except ValueError as exc:
            raise ValueError(f"Benchmark case {idx} ({path}): {exc}") from exc
    return normalized
