"""Failure injection configures an FRR router in one session, one mgmtd commit."""

from __future__ import annotations

import shlex

import pytest

from nika.problems.support.frr import frr_config_command
from nika.traffic.workload import routine_change_processes

pytestmark = pytest.mark.unit


def test_config_command_is_one_file_locked_session() -> None:
    cmd = frr_config_command("router bgp 65001", "no network 10.0.0.0/24")
    words = shlex.split(cmd)
    assert words[0] == "vtysh"
    assert cmd.count("vtysh") == 1
    assert words[1::2] == ["-c"] * 5
    assert words[2::2] == [
        "configure terminal file-lock",
        "router bgp 65001",
        "no network 10.0.0.0/24",
        "mgmt commit apply",
        "end",
    ]


def test_routine_changes_cover_each_router_once() -> None:
    processes = routine_change_processes(["r2", "r1"])
    assert [p.node for p in processes] == ["r1", "r2"]
    assert {p.command for p in processes} == {"change-window"}
    assert processes[0].args != processes[1].args
