"""Unit tests for multi-fault benchmark row normalization."""

from __future__ import annotations

from nika.workflows.benchmark.load_config import normalize_benchmark_row
from nika.workflows.benchmark.resume import benchmark_row_fingerprint
from nika.workflows.benchmark.task_label import format_task_label


def test_normalize_multi_fault_row() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos",
            "topo_size": "s",
            "problems": [
                "mtu_mismatch",
                "icmp_frag_needed_filter_misconfiguration",
            ],
            "inject": {
                "mtu_mismatch": {
                    "host_name": "leaf_router_0_1",
                    "intf_name": "eth2",
                    "mtu": "500",
                },
                "icmp_frag_needed_filter_misconfiguration": {
                    "host_name": "leaf_router_0_1",
                },
            },
        }
    )
    assert row["problems"] == [
        "mtu_mismatch",
        "icmp_frag_needed_filter_misconfiguration",
    ]
    assert row["problem"] == (
        "mtu_mismatch+icmp_frag_needed_filter_misconfiguration"
    )
    assert isinstance(row["inject"]["mtu_mismatch"], dict)


def test_task_label_for_multi_fault_problem() -> None:
    label = format_task_label(
        "dc_clos",
        "mtu_mismatch+icmp_frag_needed_filter_misconfiguration",
        "s",
    )
    assert label == "dc_clos_s_mtu_mismatch+icmp_frag_needed_filter_misconfiguration"


def test_fingerprint_includes_nested_inject() -> None:
    row = normalize_benchmark_row(
        {
            "scenario": "dc_clos",
            "topo_size": "s",
            "problems": ["link_down", "host_missing_ip"],
            "inject": {
                "link_down": {"host_name": "client_0", "intf_name": "eth0"},
                "host_missing_ip": {
                    "host_name": "webserver0_pod0",
                    "intf_name": "eth0",
                },
            },
        }
    )
    fp_a = benchmark_row_fingerprint(row)
    fp_b = benchmark_row_fingerprint(dict(row))
    assert fp_a == fp_b
