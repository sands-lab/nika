"""Generate benchmark_full.yaml and benchmark_selected.yaml from prob_pool and net_env_pool."""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

import yaml

from nika.net_env.net_env_pool import list_all_net_envs, scenario_requires_topo_size
from nika.problems.prob_pool import list_avail_problem_instances

cur_path = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, cur_path)
from inject_resolve import (  # noqa: E402
    DEFAULT_SEED,
    resolve_inject_params,
    validate_benchmark_case,
)

# One best-matching traditional Kathara scenario per failure (k8s/llmd appear in full only).
SELECTED_SCENARIO_FOR_PROBLEM: dict[str, str] = {
    "arp_acl_block": "ospf_enterprise_dhcp",
    "arp_cache_poisoning": "ospf_enterprise_dhcp",
    "bgp_acl_block": "dc_clos_bgp",
    "bgp_asn_misconfig": "dc_clos_bgp",
    "bgp_blackhole_route_leak": "dc_clos_bgp",
    "bgp_hijacking": "dc_clos_service",
    "bgp_missing_route_advertisement": "dc_clos_bgp",
    "bmv2_switch_down": "p4_bloom_filter",
    "dhcp_missing_subnet": "ospf_enterprise_dhcp",
    "dhcp_service_down": "ospf_enterprise_dhcp",
    "dhcp_spoofed_dns": "ospf_enterprise_dhcp",
    "dhcp_spoofed_gateway": "ospf_enterprise_dhcp",
    "dhcp_spoofed_subnet": "ospf_enterprise_dhcp",
    "dns_lookup_latency": "ospf_enterprise_dhcp",
    "dns_port_blocked": "ospf_enterprise_dhcp",
    "dns_record_error": "ospf_enterprise_dhcp",
    "dns_service_down": "ospf_enterprise_dhcp",
    "flow_rule_loop": "sdn_clos",
    "flow_rule_shadowing": "sdn_clos",
    "frr_service_down": "ospf_enterprise_dhcp",
    "host_crash": "dc_clos_bgp",
    "host_incorrect_dns": "ospf_enterprise_dhcp",
    "host_incorrect_gateway": "ospf_enterprise_dhcp",
    "host_incorrect_ip": "ospf_enterprise_static",
    "host_incorrect_netmask": "ospf_enterprise_static",
    "host_ip_conflict": "dc_clos_bgp",
    "host_missing_ip": "ospf_enterprise_static",
    "host_static_blackhole": "dc_clos_bgp",
    "host_vpn_membership_missing": "rip_small_internet_vpn",
    "http_acl_block": "ospf_enterprise_dhcp",
    "icmp_acl_block": "ospf_enterprise_dhcp",
    "incast_traffic_network_limitation": "ospf_enterprise_dhcp",
    "link_bandwidth_throttling": "dc_clos_bgp",
    "link_detach": "dc_clos_bgp",
    "link_down": "dc_clos_bgp",
    "link_flap": "dc_clos_bgp",
    "link_fragmentation_disabled": "dc_clos_bgp",
    "link_high_packet_corruption": "dc_clos_bgp",
    "load_balancer_overload": "ospf_enterprise_dhcp",
    "mac_address_conflict": "ospf_enterprise_dhcp",
    "mpls_label_limit_exceeded": "p4_mpls",
    "ospf_acl_block": "ospf_enterprise_dhcp",
    "ospf_area_misconfiguration": "ospf_enterprise_dhcp",
    "ospf_neighbor_missing": "ospf_enterprise_dhcp",
    "p4_aggressive_detection_thresholds": "p4_bloom_filter",
    "p4_compilation_error_parser_state": "p4_bloom_filter",
    "p4_header_definition_error": "p4_bloom_filter",
    "p4_table_entry_misconfig": "p4_bloom_filter",
    "p4_table_entry_missing": "p4_bloom_filter",
    "receiver_resource_contention": "ospf_enterprise_dhcp",
    "sdn_controller_crash": "sdn_clos",
    "sender_application_delay": "ospf_enterprise_dhcp",
    "sender_resource_contention": "ospf_enterprise_dhcp",
    "southbound_port_block": "sdn_clos",
    "southbound_port_mismatch": "sdn_clos",
    "web_dos_attack": "ospf_enterprise_dhcp",
}

# Problems whose only compatible scenarios are the Kubernetes labs. Those labs
# are full-matrix only (a single case costs a full k3s cluster bring-up), so they
# are skipped when building benchmark_selected.yaml instead of being mapped.
FULL_ONLY_PROBLEMS: set[str] = {
    "k8s_clusterip_routing_broken",
    "k8s_coredns_isolated",
    "k8s_worker_apiserver_partition",
}


def _topo_sizes_for_scenario(scenario: str) -> list[str]:
    if scenario_requires_topo_size(scenario):
        return ["s", "m", "l"]
    return [""]


def _make_row(scenario: str, problem: str, topo_size: str, *, seed: int) -> dict:
    inject = resolve_inject_params(problem, scenario, topo_size, seed=seed)
    validate_benchmark_case(scenario, problem, inject, topo_size)
    return {
        "scenario": scenario,
        "topo_size": topo_size or None,
        "problem": problem,
        "inject": inject,
    }


def iter_full_cases(*, seed: int) -> list[dict]:
    net_envs = list_all_net_envs()
    problem_instances = list_avail_problem_instances()
    rows: list[dict] = []

    for prob_name, problem_class in problem_instances.items():
        problem_instance = problem_class
        for net_env_name, net_env_cls in net_envs.items():
            if not set(problem_instance.TAGS).issubset(set(net_env_cls.TAGS)):
                continue
            for topo_size in _topo_sizes_for_scenario(net_env_name):
                rows.append(_make_row(net_env_name, prob_name, topo_size, seed=seed))
    return rows


def iter_selected_cases(*, seed: int) -> list[dict]:
    net_envs = list_all_net_envs()
    problem_instances = list_avail_problem_instances()
    rows: list[dict] = []

    for prob_name in sorted(problem_instances.keys()):
        if prob_name in FULL_ONLY_PROBLEMS:
            continue
        scenario = SELECTED_SCENARIO_FOR_PROBLEM.get(prob_name)
        if scenario is None:
            raise ValueError(f"No selected scenario mapping for problem {prob_name!r}")
        net_env_cls = net_envs[scenario]
        problem_instance = problem_instances[prob_name]
        if not set(problem_instance.TAGS).issubset(set(net_env_cls.TAGS)):
            raise ValueError(
                f"Selected scenario {scenario} not tag-compatible with {prob_name} "
                f"(problem={problem_instance.TAGS}, scenario={net_env_cls.TAGS})"
            )
        topo_size = "s" if scenario_requires_topo_size(scenario) else ""
        rows.append(_make_row(scenario, prob_name, topo_size, seed=seed))
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


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate benchmark YAML configs.")
    parser.add_argument(
        "--seed",
        type=int,
        default=DEFAULT_SEED,
        help=f"Global random seed for inject param selection (default: {DEFAULT_SEED})",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    generate_benchmark(seed=args.seed)
