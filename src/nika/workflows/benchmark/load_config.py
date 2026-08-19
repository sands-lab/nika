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
from nika.problems.prob_pool import resolve_problem_name


def _site_edge_wg_inject(topo_size: str) -> dict[str, str]:
    """Default Branch→HQ WireGuard target for remapped legacy VPN cases."""
    from nika.net_env.kathara.enterprise_wan.enterprise_branch.topology import (
        single_path_hq_peer_targets,
    )

    size = topo_size if topo_size in {"s", "m", "l"} else "s"
    targets = single_path_hq_peer_targets(size)  # type: ignore[arg-type]
    if not targets:
        raise ValueError(
            f"No single-path HQ WireGuard peers for enterprise_branch "
            f"topo_size={size!r}"
        )
    edge, iface = targets[0]
    return {"host_name": edge, "intf_name": iface}


def normalize_benchmark_row(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize scenario aliases, failure aliases, and ``workload`` on one case."""
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
    topo_size = "" if topo in ("-", "", None) else str(topo)

    raw_problem = str(row["problem"])
    problem = resolve_problem_name(raw_problem)
    inject = row.get("inject") or {}
    if not isinstance(inject, dict):
        raise ValueError("'inject' must be a mapping")

    # Legacy host-VPN inject params do not apply to Site Edge tunnels.
    if raw_problem == "host_vpn_membership_missing":
        inject = _site_edge_wg_inject(topo_size)
        root_causes = [
            {
                "resource": {
                    "kind": "interface",
                    "node": inject["host_name"],
                    "name": inject["intf_name"],
                },
                "fault_type": problem,
            }
        ]
    else:
        if not inject:
            raise ValueError(
                f"Case {canonical}/{raw_problem} missing non-empty 'inject' map"
            )
        inject = {str(k): str(v) for k, v in inject.items()}
        root_causes = row.get("root_causes")

    normalized: dict[str, Any] = {
        "scenario": canonical,
        "problem": problem,
        "topo_size": topo_size,
        "inject": inject,
    }
    if canonical in (DC_CLOS_SCENARIO, CAMPUS_LAN_SCENARIO):
        normalized["workload"] = workload
    if root_causes:
        normalized["root_causes"] = root_causes
    if row.get("root_causes_status") and raw_problem != "host_vpn_membership_missing":
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
    scenario with the matching ``workload``. Legacy
    ``host_vpn_membership_missing`` rewrites to
    ``wireguard_peer_key_misconfiguration`` with a Site Edge inject target.
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
