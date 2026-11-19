"""The tested multi-fault catalog and the checks for user combinations."""

from __future__ import annotations

import pytest

from nika.problems.combinations import (
    check_multi_fault_task,
    combination_for_row,
    get_combination,
    list_combinations,
)
from nika.problems.rca.inventory import load_offline_net_env
from nika.workflows.benchmark.load_config import (
    combination_rows,
    normalize_benchmark_row,
)

pytestmark = pytest.mark.contract

TASKS = [
    (combination, row)
    for combination in list_combinations()
    for row in combination.rows()
]


def _id(item):
    combination, row = item
    return "-".join(
        [combination.name, row["scenario"], row.get("device_profile", "default")]
    )


@pytest.mark.parametrize("item", TASKS, ids=_id)
def test_catalog_tasks_inject_and_match_their_combination(item):
    combination, row = item
    check_multi_fault_task(row)
    assert combination_for_row(normalize_benchmark_row(row)) == combination.name


def test_user_combinations_are_checked_but_not_restricted():
    free = {
        "scenario": "dc_clos",
        "topo_size": "s",
        "problems": ["link_down", "host_missing_ip"],
        "inject": {
            "link_down": {"host_name": "client_1_1", "intf_name": "eth0"},
            "host_missing_ip": {"host_name": "client_2_1", "intf_name": "eth0"},
        },
    }
    check_multi_fault_task(free)
    assert combination_for_row(normalize_benchmark_row(free)) is None
    broken = {
        **free,
        "inject": {
            **free["inject"],
            "host_missing_ip": {"host_name": "no_such_host", "intf_name": "eth0"},
        },
    }
    with pytest.raises(ValueError, match="host_missing_ip"):
        check_multi_fault_task(broken)


def test_release_admits_only_catalog_tasks():
    from nika.workflows.benchmark.release import (
        ReleaseError,
        _validate_release_combinations,
    )

    combination = get_combination("redundant_links_both_lost")
    tested = normalize_benchmark_row(combination.rows()[0])
    _validate_release_combinations([tested])
    moved = normalize_benchmark_row(
        {
            **combination.rows()[0],
            "inject": {
                name: {**params, "host_name": "elsewhere"}
                for name, params in combination.rows()[0]["inject"].items()
            },
        }
    )
    with pytest.raises(ReleaseError, match="tested catalog tasks"):
        _validate_release_combinations([tested, moved])


def test_timed_combinations_start_diagnosis_after_the_network_settles():
    combination = get_combination("standby_tunnel_key_exposed")
    schedule = combination.rows()[0]["schedule"]
    assert [event["at"] for event in schedule["events"]] == ["0s", "30s"]
    assert schedule["agent_start"] == "210s"


def test_timed_and_untimed_cases_load_as_tasks():
    causes = [
        {"resource": {"kind": "node", "node": "leaf_1"}, "fault_type": "x"},
    ]
    untimed, timed = combination_rows(
        "dc_clos",
        {
            "name": "pmtud_black_hole",
            "failures": ["mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"],
            "cases": [
                {
                    "topo_size": "s",
                    "objectives": ["rca"],
                    "inject": {
                        "mtu_mismatch": {
                            "host_name": "leaf_1",
                            "intf_name": "eth0",
                            "mtu": "500",
                        },
                        "icmp_frag_needed_filter_misconfiguration": {
                            "host_name": "leaf_1"
                        },
                    },
                    "root_causes": causes,
                },
                {
                    "topo_size": "s",
                    "objectives": ["rca"],
                    "schedule": {
                        "agent_start": "30s",
                        "events": [
                            {
                                "problem": "mtu_mismatch",
                                "inject": {
                                    "host_name": "leaf_1",
                                    "intf_name": "eth0",
                                    "mtu": "500",
                                },
                                "at": "0s",
                            },
                            {
                                "problem": "icmp_frag_needed_filter_misconfiguration",
                                "inject": {"host_name": "leaf_1"},
                                "at": "30s",
                            },
                        ],
                    },
                    "root_causes": causes,
                },
            ],
        },
    )
    untimed = normalize_benchmark_row(untimed)
    timed = normalize_benchmark_row(timed)
    assert untimed["objectives"] == timed["objectives"] == ["rca"]
    assert untimed["problems"] == timed["problems"]
    assert untimed["inject"]["mtu_mismatch"]["host_name"] == "leaf_1"
    assert timed["schedule"]["agent_start"]


def test_session_policy_uses_declared_overlay_interface_for_root_cause():
    from types import SimpleNamespace

    from nika.problems.routing_control_plane.bgp import BGPSessionPolicyMissing

    env = load_offline_net_env("enterprise_branch", "s", device_profile="routeros")
    problem = BGPSessionPolicyMissing.__new__(BGPSessionPolicyMissing)
    problem.net_env = env
    inject = {"host_name": "br1_edge", "peer": "172.30.0.9"}
    problem.validate_combined_inject(SimpleNamespace(net_env=env), inject)
    (resource,) = problem.root_cause_resources(problem.parse_params(inject))
    assert resource.kind == "interface"
    assert resource.node == "br1_edge"
    assert resource.name == "wg_hq"


def test_catalog_matching_checks_event_offsets_and_agent_start():
    from copy import deepcopy

    row = normalize_benchmark_row(get_combination("rov_gap_admits_hijack").rows()[0])
    assert combination_for_row(row) == "rov_gap_admits_hijack"
    delayed = deepcopy(row)
    delayed["schedule"]["events"][1]["at"] += 5
    delayed["schedule"]["agent_start"] += 5
    assert combination_for_row(delayed) is None
    early_agent = deepcopy(row)
    early_agent["schedule"]["agent_start"] += 5
    assert combination_for_row(early_agent) is None


def test_combination_candidates_enable_every_telemetry_kind():
    from nika.telemetry import TELEMETRY_KINDS
    from nika.workflows.benchmark.generate import build_combination_documents

    documents = build_combination_documents()
    assert documents
    for document in documents:
        for case in document["combination"]["cases"]:
            assert set(case["telemetry"]) == set(TELEMETRY_KINDS)


@pytest.mark.parametrize("missing", ["host_missing_ip", "dns_record_error"])
def test_combination_requires_parameters_for_each_declared_failure(missing):
    row = {
        "scenario": "campus_lan",
        "topo_size": "s",
        "problems": ["host_missing_ip", "dns_record_error"],
        "inject": {
            "host_missing_ip": {"host_name": "client_2_1", "intf_name": "eth0"},
            "dns_record_error": {"host_name": "dns_server"},
        },
    }
    del row["inject"][missing]
    with pytest.raises(ValueError, match="every declared failure"):
        check_multi_fault_task(row)
