"""Routine FRR edits stay benign: interface descriptions and an unused list."""

from __future__ import annotations

import random
import re

import pytest

from nika.traffic.apps import change_window as cw

pytestmark = pytest.mark.unit

RUNNING_CONFIG = """\
interface eth0
 ip address 10.0.0.5/31
exit
!
interface eth1
 ip ospf cost 10
exit
!
interface lo
 ip address 10.255.0.6/32
exit
!
router bgp 65003
"""


def test_interfaces_skip_loopback() -> None:
    assert cw.interfaces_from_config(RUNNING_CONFIG) == ["eth0", "eth1"]


def test_changes_never_touch_routing_state() -> None:
    rng = random.Random(7)
    described: dict[str, str] = {}
    listed: set[int] = set()
    seen_kinds = set()
    for _ in range(300):
        lines = cw.choose_change(rng, ["eth0", "eth1"], described, listed)
        if lines[0].startswith("interface "):
            assert lines[1] == "no description" or lines[1].startswith("description ")
            seen_kinds.add("description")
        else:
            assert len(lines) == 1
            assert re.fullmatch(
                rf"(no )?ip prefix-list {cw.REFERENCE_LIST} seq \d+ permit "
                r"(192\.0\.2|198\.51\.100|203\.0\.113)\.0/24",
                lines[0],
            )
            seen_kinds.add("prefix-list")
    assert seen_kinds == {"description", "prefix-list"}


def test_prefix_list_state_matches_edits() -> None:
    rng = random.Random(1)
    listed: set[int] = set()
    for _ in range(200):
        (line,) = cw.choose_change(rng, [], {}, listed)
        seq = int(re.search(r"seq (\d+)", line).group(1))
        assert (seq in listed) == (not line.startswith("no "))


def test_edits_are_seeded() -> None:
    def run(seed: int) -> list[list[str]]:
        rng = random.Random(seed)
        state: tuple[dict[str, str], set[int]] = ({}, set())
        return [cw.choose_change(rng, ["eth0", "eth1"], *state) for _ in range(20)]

    assert run(3) == run(3)
    assert run(3) != run(4)
