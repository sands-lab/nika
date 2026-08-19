"""Generate working benchmark YAML, or freeze a Dev/Test release from it.

Default: write ``benchmark_full.yaml`` and ``benchmark_selected.yaml`` from the
live problem and scenario registries. ``--release VERSION`` writes
``benchmark/releases/VERSION`` from those files (Dev = selected, Test = held-out
instances from full). It does not regenerate the working matrices.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml

from nika.config import BENCHMARK_DIR
from nika.net_env.net_env_pool import (
    is_dc_clos_scenario,
    is_campus_lan_scenario,
    list_all_net_envs,
    scenario_requires_topo_size,
)
from nika.problems.ground_truth import ground_truth_for_case
from nika.problems.prob_pool import list_avail_problem_instances
from nika.problems.root_cause import UnresolvedRootCauseError, canonical_root_causes
from nika.problems.topology_inventory import load_offline_net_env
from nika.workflows.benchmark.compatibility import (
    _PROBLEM_COLUMN_ALLOWLIST,
    parse_column,
)
from nika.workflows.benchmark.isp_options import (
    ISP_SCENARIO,
    isp_config_for_problem,
)
from nika.workflows.benchmark.load_config import load_benchmark_yaml
from nika.workflows.benchmark.migrate import materialize_cases, write_cases_yaml
from nika.workflows.benchmark.release import (
    DEFAULTS_V1,
    RESOURCES_V1,
    SCORING,
    TOOLS_V1,
    build_scenario_problem_pins,
    collect_images_for_scenarios,
    releases_dir,
    verify_dev_test_isolation,
    write_release_manifest,
)
from nika.workflows.benchmark.resume import benchmark_row_fingerprint

# RPKI capability: representative SNDlib topologies (not a full cartesian product).
RPKI_SELECTED_TOPOS: tuple[str, ...] = ("abilene", "geant")


def _rpki_isp_options(topo: str) -> dict:
    return {
        "topo": topo,
        "igp": "ospf",
        "bgp_mode": "ebgp",
        "rpki": True,
    }


cur_path = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_path)
from inject_resolve import (  # noqa: E402
    DEFAULT_SEED,
    resolve_inject_params,
    validate_benchmark_case,
)

# One best-matching traditional Kathara scenario per failure (k8s/llmd appear in full only).
SELECTED_SCENARIO_FOR_PROBLEM: dict[str, str] = {
    "arp_acl_block": "campus_lan",
    "arp_cache_poisoning": "campus_lan",
    "bgp_acl_block": "dc_clos",
    "bgp_asn_misconfig": "dc_clos",
    "bgp_blackhole_route_leak": "dc_clos",
    "bgp_hijacking": "dc_clos",
    "bgp_missing_route_advertisement": "dc_clos",
    "bgp_rpki_invalid_route_leak": "isp",
    "bgp_max_prefix_exceeded": "isp",
    "bmv2_switch_down": "p4_bloom_filter",
    "dhcp_missing_subnet": "campus_lan",
    "dhcp_service_down": "campus_lan",
    "dhcp_spoofed_dns": "campus_lan",
    "dhcp_spoofed_gateway": "campus_lan",
    "dhcp_spoofed_subnet": "campus_lan",
    "dns_lookup_latency": "campus_lan",
    "dns_port_blocked": "campus_lan",
    "dns_record_error": "campus_lan",
    "dns_service_down": "campus_lan",
    "flow_rule_loop": "sdn_l3_clos",
    "flow_rule_shadowing": "sdn_l3_clos",
    "frr_service_down": "campus_lan",
    "host_crash": "dc_clos",
    "host_incorrect_dns": "campus_lan",
    "host_incorrect_gateway": "campus_lan",
    "host_incorrect_ip": "campus_lan",
    "host_incorrect_netmask": "campus_lan",
    "host_ip_conflict": "dc_clos",
    "host_missing_ip": "campus_lan",
    "host_static_blackhole": "dc_clos",
    "http_acl_block": "campus_lan",
    "icmp_acl_block": "campus_lan",
    "incast_traffic_network_limitation": "campus_lan",
    "link_bandwidth_throttling": "dc_clos",
    "link_detach": "dc_clos",
    "link_down": "dc_clos",
    "link_flap": "dc_clos",
    "mtu_mismatch": "dc_clos",
    "link_high_packet_corruption": "dc_clos",
    "load_balancer_overload": "campus_lan",
    "mac_address_conflict": "campus_lan",
    "mpls_label_limit_exceeded": "p4_mpls",
    "ospf_acl_block": "campus_lan",
    "ospf_area_misconfiguration": "campus_lan",
    "ospf_neighbor_missing": "campus_lan",
    "p4_aggressive_detection_thresholds": "p4_bloom_filter",
    "p4_compilation_error_parser_state": "p4_bloom_filter",
    "p4_header_definition_error": "p4_bloom_filter",
    "p4_table_entry_misconfig": "p4_bloom_filter",
    "p4_table_entry_missing": "p4_bloom_filter",
    "p4_action_selector_member_misconfig": "p4_dc_fabric",
    "p4_ecmp_group_member_missing": "p4_dc_fabric",
    "p4runtime_pipeline_mismatch": "p4_dc_fabric",
    "p4runtime_partial_write": "p4_dc_fabric",
    "p4_table_resource_exhaustion": "p4_dc_fabric",
    "receiver_resource_contention": "campus_lan",
    "sdn_controller_crash": "sdn_l3_clos",
    "sender_application_delay": "campus_lan",
    "sender_resource_contention": "campus_lan",
    "southbound_port_block": "sdn_l3_clos",
    "southbound_port_mismatch": "sdn_l3_clos",
    "web_dos_attack": "campus_lan",
    "wireguard_allowed_ips_misconfiguration": "enterprise_branch",
    "wireguard_peer_key_misconfiguration": "enterprise_branch",
    "vrf_dscp_remarking": "enterprise_branch",
}

# Failures that are tag-compatible with host Clos but prefer service endpoints.
DC_CLOS_SERVICE_WORKLOAD_OVERRIDES: frozenset[str] = frozenset({"bgp_hijacking"})

# Host-address faults that need sticky static IPs on the enterprise lab.
OSPF_STATIC_WORKLOAD_OVERRIDES: frozenset[str] = frozenset(
    {
        "host_incorrect_ip",
        "host_incorrect_netmask",
        "host_missing_ip",
    }
)


def workload_for_dc_clos(problem: str, problem_tags: set[str]) -> str:
    """Pick Clos workload from failure observation needs (not a cartesian product)."""
    if problem in DC_CLOS_SERVICE_WORKLOAD_OVERRIDES:
        return "service"
    if problem_tags & {"dns", "http"}:
        return "service"
    return "host"


def workload_for_campus_lan(problem: str, problem_tags: set[str]) -> str:
    """Pick campus_lan workload from failure needs (not a cartesian product)."""
    if problem in OSPF_STATIC_WORKLOAD_OVERRIDES:
        return "static"
    if problem_tags & {"dhcp", "dns", "load_balancer", "web"}:
        return "dhcp"
    return "dhcp"


# Problems whose only compatible scenarios are the Kubernetes labs. Those labs
# are full-matrix only (a single case costs a full k3s cluster bring-up), so they
# are skipped when building benchmark_selected.yaml instead of being mapped.
FULL_ONLY_PROBLEMS: set[str] = {
    "k8s_clusterip_routing_broken",
    "k8s_coredns_isolated",
    "k8s_worker_apiserver_partition",
    "k8s_networkpolicy_deny",
}

# Problems kept in the registry but excluded from working matrices.
ORPHANED_PROBLEMS: set[str] = set()


def _topo_sizes_for_scenario(scenario: str) -> list[str]:
    if scenario_requires_topo_size(scenario):
        return ["s", "m", "l"]
    return [""]


def _make_row(
    scenario: str,
    problem: str,
    topo_size: str,
    *,
    seed: int,
    workload: str | None = None,
    isp_options: dict[str, str] | None = None,
) -> dict:
    inject = resolve_inject_params(
        problem,
        scenario,
        topo_size,
        seed=seed,
        workload=workload,
        isp_options=isp_options,
    )
    validate_benchmark_case(
        scenario,
        problem,
        inject,
        topo_size,
        workload=workload,
        isp_options=isp_options,
    )
    row: dict = {
        "scenario": scenario,
        "topo_size": topo_size or None,
        "problem": problem,
        "inject": inject,
    }
    if workload is not None:
        row["workload"] = workload
    if isp_options is not None:
        row.update(isp_options)
    try:
        gt = ground_truth_for_case(
            problem=problem,
            params=inject,
            scenario=scenario,
            topo_size=topo_size,
            net_env=load_offline_net_env(
                scenario,
                topo_size,
                workload=workload,
                **(isp_options or {}),
            ),
        )
        row["root_causes"] = canonical_root_causes(gt.root_causes)
    except UnresolvedRootCauseError as exc:
        row["root_causes_status"] = "unresolved"
        row["root_causes_error"] = str(exc)
    return row


def iter_full_cases(*, seed: int) -> list[dict]:
    net_envs = list_all_net_envs()
    problem_instances = list_avail_problem_instances()
    rows: list[dict] = []

    for prob_name, problem_class in problem_instances.items():
        if prob_name in ORPHANED_PROBLEMS:
            continue
        problem_instance = problem_class
        problem_tags = set(problem_instance.TAGS)
        for net_env_name, net_env_cls in net_envs.items():
            if not problem_tags.issubset(set(net_env_cls.TAGS)):
                continue
            allow = _PROBLEM_COLUMN_ALLOWLIST.get(prob_name)
            if allow is not None and net_env_name not in {
                parse_column(column)[0] for column in allow
            }:
                continue
            workload = None
            isp_options = None
            if is_dc_clos_scenario(net_env_name):
                workload = workload_for_dc_clos(prob_name, problem_tags)
            elif is_campus_lan_scenario(net_env_name):
                workload = workload_for_campus_lan(prob_name, problem_tags)
            elif net_env_name == ISP_SCENARIO:
                if prob_name == "bgp_rpki_invalid_route_leak":
                    for topo in RPKI_SELECTED_TOPOS:
                        for topo_size in _topo_sizes_for_scenario(net_env_name):
                            rows.append(
                                _make_row(
                                    net_env_name,
                                    prob_name,
                                    topo_size,
                                    seed=seed,
                                    workload=None,
                                    isp_options=_rpki_isp_options(topo),
                                )
                            )
                    continue
                isp_options = isp_config_for_problem(prob_name, problem_tags)
            for topo_size in _topo_sizes_for_scenario(net_env_name):
                rows.append(
                    _make_row(
                        net_env_name,
                        prob_name,
                        topo_size,
                        seed=seed,
                        workload=workload,
                        isp_options=isp_options,
                    )
                )
    return rows


def iter_selected_cases(*, seed: int) -> list[dict]:
    net_envs = list_all_net_envs()
    problem_instances = list_avail_problem_instances()
    rows: list[dict] = []

    for prob_name in sorted(problem_instances.keys()):
        if prob_name in FULL_ONLY_PROBLEMS:
            continue
        if prob_name in ORPHANED_PROBLEMS:
            continue
        scenario = SELECTED_SCENARIO_FOR_PROBLEM.get(prob_name)
        if scenario is None:
            raise ValueError(f"No selected scenario mapping for problem {prob_name!r}")
        net_env_cls = net_envs[scenario]
        problem_instance = problem_instances[prob_name]
        problem_tags = set(problem_instance.TAGS)
        if not problem_tags.issubset(set(net_env_cls.TAGS)):
            raise ValueError(
                f"Selected scenario {scenario} not tag-compatible with {prob_name} "
                f"(problem={problem_instance.TAGS}, scenario={net_env_cls.TAGS})"
            )
        topo_size = "s" if scenario_requires_topo_size(scenario) else ""
        workload = None
        isp_options = None
        if is_dc_clos_scenario(scenario):
            workload = workload_for_dc_clos(prob_name, problem_tags)
        elif is_campus_lan_scenario(scenario):
            workload = workload_for_campus_lan(prob_name, problem_tags)
        elif scenario == ISP_SCENARIO:
            if prob_name == "bgp_rpki_invalid_route_leak":
                for topo in RPKI_SELECTED_TOPOS:
                    rows.append(
                        _make_row(
                            scenario,
                            prob_name,
                            topo_size,
                            seed=seed,
                            workload=None,
                            isp_options=_rpki_isp_options(topo),
                        )
                    )
                continue
            isp_options = isp_config_for_problem(prob_name, problem_tags)
        rows.append(
            _make_row(
                scenario,
                prob_name,
                topo_size,
                seed=seed,
                workload=workload,
                isp_options=isp_options,
            )
        )
    return rows


def _print_stats(label: str, rows: list[dict]) -> None:
    by_scenario = Counter(r["scenario"] for r in rows)
    by_problem = Counter(r["problem"] for r in rows)
    print(
        f"\n{label}: {len(rows)} cases, {len(by_problem)} problems, {len(by_scenario)} scenarios"
    )
    for scenario, count in sorted(by_scenario.items(), key=lambda x: (-x[1], x[0])):
        print(f"  {scenario}: {count}")


def generate_benchmark(*, seed: int = DEFAULT_SEED) -> tuple[list[dict], list[dict]]:
    full_rows = iter_full_cases(seed=seed)
    selected_rows = iter_selected_cases(seed=seed)

    _print_stats("benchmark_full.yaml", full_rows)
    _print_stats("benchmark_selected.yaml", selected_rows)

    benchmark_dir = Path(cur_path)
    for name, rows in (
        ("benchmark_full.yaml", full_rows),
        ("benchmark_selected.yaml", selected_rows),
    ):
        out_path = benchmark_dir / name
        out_path.write_text(
            yaml.dump(
                {"seed": seed, "cases": rows},
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
        print(f"Wrote {len(rows)} cases to {out_path} (seed={seed})")

    return full_rows, selected_rows


HELDOUT_SEED = 43


def _normalize_row(row: dict) -> dict:
    topo = row.get("topo_size")
    return {
        "scenario": str(row["scenario"]),
        "topo_size": None if topo in ("", None, "-") else str(topo),
        "problem": str(row["problem"]),
        "inject": {str(k): str(v) for k, v in (row.get("inject") or {}).items()},
    }


def _rank_candidate(dev: dict, cand: dict) -> tuple[int, int, str]:
    """Lower is better: prefer different scenario, then topo, then stable id."""
    same_scenario = 1 if cand["scenario"] == dev["scenario"] else 0
    same_topo = (
        1 if (cand.get("topo_size") or "") == (dev.get("topo_size") or "") else 0
    )
    key = (
        f"{cand['scenario']}|{cand.get('topo_size') or ''}|"
        f"{benchmark_row_fingerprint(cand)}"
    )
    return (same_scenario, same_topo, key)


def select_heldout_cases(
    *,
    dev_cases: list[dict],
    full_cases: list[dict],
    heldout_seed: int = HELDOUT_SEED,
) -> tuple[list[dict], list[str]]:
    """Return (test_rows, fallback_problems)."""
    full_by_problem: dict[str, list[dict]] = defaultdict(list)
    for row in full_cases:
        full_by_problem[str(row["problem"])].append(_normalize_row(row))

    test_rows: list[dict] = []
    fallbacks: list[str] = []

    for dev_raw in dev_cases:
        dev = _normalize_row(dev_raw)
        problem = dev["problem"]
        dev_fp = benchmark_row_fingerprint(dev)
        candidates = [
            c
            for c in full_by_problem.get(problem, [])
            if benchmark_row_fingerprint(c) != dev_fp
        ]
        if candidates:
            candidates.sort(key=lambda c: _rank_candidate(dev, c))
            test_rows.append(candidates[0])
            continue

        topo = "" if not dev.get("topo_size") else str(dev["topo_size"])
        alt = None
        for seed in range(heldout_seed, heldout_seed + 32):
            inject = resolve_inject_params(problem, dev["scenario"], topo, seed=seed)
            validate_benchmark_case(dev["scenario"], problem, inject, topo)
            candidate = {
                "scenario": dev["scenario"],
                "topo_size": dev.get("topo_size"),
                "problem": problem,
                "inject": {str(k): str(v) for k, v in inject.items()},
            }
            if benchmark_row_fingerprint(candidate) != dev_fp:
                alt = candidate
                break
        if alt is None:
            from nika.net_env.net_env_pool import get_net_env_instance

            env = get_net_env_instance(dev["scenario"])
            machines = sorted(env.lab.machines.keys()) if env.lab else []
            base_inject = dict(dev["inject"])
            host_key = next(
                (k for k in ("host_name", "attacker_device") if k in base_inject),
                None,
            )
            if host_key is None or not machines:
                raise RuntimeError(
                    f"Cannot synthesize held-out instance for {problem!r} "
                    f"on {dev['scenario']!r}"
                )
            for machine in machines:
                if str(machine) == str(base_inject[host_key]):
                    continue
                trial = dict(base_inject)
                trial[host_key] = str(machine)
                validate_benchmark_case(dev["scenario"], problem, trial, topo)
                candidate = {
                    "scenario": dev["scenario"],
                    "topo_size": dev.get("topo_size"),
                    "problem": problem,
                    "inject": trial,
                }
                if benchmark_row_fingerprint(candidate) != dev_fp:
                    alt = candidate
                    break
        if alt is None:
            raise RuntimeError(
                f"Held-out fallback for {problem!r} still matches Dev fingerprint"
            )
        test_rows.append(alt)
        fallbacks.append(problem)

    verify_dev_test_isolation(dev_cases=dev_cases, test_cases=test_rows)
    return test_rows, fallbacks


def generate_release_splits(
    *,
    version: str,
    selected_path: Path | None = None,
    full_path: Path | None = None,
    out_dir: Path | None = None,
) -> Path:
    selected_path = selected_path or (BENCHMARK_DIR / "benchmark_selected.yaml")
    full_path = full_path or (BENCHMARK_DIR / "benchmark_full.yaml")
    dest = out_dir or (releases_dir() / version)
    dest.mkdir(parents=True, exist_ok=True)

    selected_raw = yaml.safe_load(selected_path.read_text(encoding="utf-8"))
    if not isinstance(selected_raw, dict) or "cases" not in selected_raw:
        raise ValueError(
            f"Invalid selected YAML (missing top-level 'cases'): {selected_path}"
        )

    dev_path = dest / "dev.yaml"
    dev_cases = materialize_cases(list(selected_raw.get("cases") or []))
    write_cases_yaml(dev_path, seed=selected_raw.get("seed"), cases=dev_cases)
    legacy = dest / "cases.yaml"
    if legacy.is_file() and legacy.resolve() != dev_path.resolve():
        legacy.unlink()

    full_cases = load_benchmark_yaml(full_path)
    test_identity, fallbacks = select_heldout_cases(
        dev_cases=dev_cases, full_cases=full_cases
    )
    test_cases = materialize_cases(test_identity)
    test_path = dest / "test.yaml"
    write_cases_yaml(test_path, seed=HELDOUT_SEED, cases=test_cases)

    dev_sha = hashlib.sha256(dev_path.read_bytes()).hexdigest()
    test_sha = hashlib.sha256(test_path.read_bytes()).hexdigest()

    scenarios = {row["scenario"] for row in dev_cases} | {
        row["scenario"] for row in test_cases
    }
    problems = {row["problem"] for row in dev_cases}
    pins = build_scenario_problem_pins(scenarios, problems)
    images = {"required": collect_images_for_scenarios(scenarios)}
    splits = {
        "dev": {
            "cases_file": "dev.yaml",
            "case_count": len(dev_cases),
            "cases_sha256": dev_sha,
        },
        "test": {
            "cases_file": "test.yaml",
            "case_count": len(test_cases),
            "cases_sha256": test_sha,
        },
    }
    digest = write_release_manifest(
        dest,
        version=version,
        splits=splits,
        defaults=dict(DEFAULTS_V1),
        scoring=dict(SCORING),
        tools=dict(TOOLS_V1),
        resources=dict(RESOURCES_V1),
        images=images,
        scenario_problem_pin=pins,
    )

    print(f"Wrote {dest}")
    print(f"  dev:  {len(dev_cases)} cases sha256={dev_sha[:12]}…")
    print(f"  test: {len(test_cases)} cases sha256={test_sha[:12]}…")
    print(f"  benchmark_digest={digest}")
    if fallbacks:
        print(f"  seed={HELDOUT_SEED} inject fallbacks: {', '.join(fallbacks)}")
    else:
        print("  no inject-seed fallbacks needed")
    return dest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Inject seed for working matrices (default: {DEFAULT_SEED})",
    )
    parser.add_argument(
        "--release",
        metavar="VERSION",
        default=None,
        help=(
            "Write benchmark/releases/VERSION from current working YAML. "
            "Does not regenerate full/selected. Do not reuse 0.1.0."
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Release output directory (default: benchmark/releases/<version>)",
    )
    args = parser.parse_args(argv)
    if args.out_dir is not None and args.release is None:
        parser.error("--out-dir requires --release")
    if args.release:
        generate_release_splits(version=args.release, out_dir=args.out_dir)
    else:
        generate_benchmark(seed=args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
