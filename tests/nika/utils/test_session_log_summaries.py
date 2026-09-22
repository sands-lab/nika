"""Unit tests for operator session log summaries."""

from __future__ import annotations

from nika.utils.session_log_summaries import (
    failed_checks_map,
    summarize_fault_verify,
    summarize_injection,
    summarize_lab_verify,
)


def test_summarize_lab_verify_includes_checks_and_details() -> None:
    summary = summarize_lab_verify(
        {
            "verified": True,
            "scenario_name": "dc_clos",
            "checks": {"nodes_deployed": True, "client_ipv4": True},
            "details": {"client_ipv4": {"host": "client_0", "address": "192.168.0.2"}},
        }
    )
    assert "scenario=dc_clos" in summary
    assert "passed" in summary
    assert "nodes_deployed" in summary
    assert "client_0" in summary


def test_failed_checks_map_filters_truthy() -> None:
    assert failed_checks_map({"a": True, "b": False, "c": False}) == {
        "b": False,
        "c": False,
    }


def test_summarize_injection_uses_resolved_params() -> None:
    summary = summarize_injection(
        "link_down",
        {
            "problem_class": "LinkDown",
            "resolved_params": {"host_name": "leaf_router_0_0", "intf_name": "eth1"},
        },
    )
    assert "link_down" in summary
    assert "host_name=leaf_router_0_0" in summary
    assert "intf_name=eth1" in summary


def test_summarize_fault_verify_includes_details() -> None:
    summary = summarize_fault_verify(
        {
            "verified": True,
            "fault_type": "link_down",
            "details": {"host": "leaf_router_0_0", "intf": "eth1", "operstate": "down"},
        }
    )
    assert "link_down" in summary
    assert "verified" in summary
    assert "operstate=down" in summary
