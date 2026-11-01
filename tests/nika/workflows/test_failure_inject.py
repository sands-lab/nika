"""Unit tests for failure-inject recorded attributes (no Docker)."""

from __future__ import annotations

import pytest

from nika.workflows.failure.inject import (
    _extract_injection_params,
    _refresh_injection_params,
)

pytestmark = pytest.mark.unit


class _RecordedFlap:
    RECORDED_ATTRS = ("_controller_target", "faulty_intf")

    def __init__(self, *, peer: str | None, intf: str = "eth0") -> None:
        self._controller_target = peer
        self.faulty_intf = intf


def test_refresh_injection_params_records_attrs_set_during_inject() -> None:
    problem = _RecordedFlap(peer="nwabc123", intf="eth1")
    row = {
        "problem_class": "_RecordedFlap",
        "_controller_target": None,
        "faulty_intf": "eth0",
        "resolved_params": {"host_name": "leaf1"},
    }
    rows = [(0, "link_flap", row)]

    _refresh_injection_params(problem, ["link_flap"], rows)

    assert row["_controller_target"] == "nwabc123"
    assert row["faulty_intf"] == "eth1"
    assert row["resolved_params"] == {"host_name": "leaf1"}


def test_extract_injection_params_before_inject_sees_unset_controller_target() -> None:
    problem = _RecordedFlap(peer=None)
    params = _extract_injection_params(problem)
    assert params["_controller_target"] is None
