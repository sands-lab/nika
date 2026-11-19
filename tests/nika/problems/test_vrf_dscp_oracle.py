"""DSCP reference evidence reads actual RouterOS packet-counter tables."""

from pathlib import Path

import pytest

from nika.problems.forwarding_encapsulation_policy.vrf_dscp import _counter_grows

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parents[2] / "fixtures"


@pytest.mark.parametrize("fixture", ["routeros-dscp-stats", "routeros-dscp-from"])
def test_rewrite_counter_reads_native_stats_columns_and_thousands(fixture):
    before = (FIXTURES / f"{fixture}-before.txt").read_text()
    after = (FIXTURES / f"{fixture}-after.txt").read_text()
    assert _counter_grows(before + "\n" + after)
    assert not _counter_grows(before + "\n" + before)
    assert not _counter_grows(after + "\n" + before)
    assert not _counter_grows(after)
    assert not _counter_grows(before.replace("change-dscp", "accept     ") + after)


@pytest.mark.parametrize(
    "reading,expected",
    [
        ("counter packets 14 bytes 1800\ncounter packets 18 bytes 2000", True),
        ("packets=14\npackets=18", True),
        ("packets=0\npackets=18", False),
        ("packets=18\npackets=18", False),
        ("syntax error", False),
    ],
)
def test_rewrite_counter_preserves_positive_growth_controls(reading, expected):
    assert _counter_grows(reading) is expected
