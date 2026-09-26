"""Startup verification heals a dead watchfrr at most once per node."""

from __future__ import annotations

import pytest
from nika.net_env.verify import frr_active_or_heal, should_heal_frr


@pytest.mark.parametrize(
    ("unit_active", "zebra_running", "already_healed", "expected"),
    [
        (False, True, False, True),
        (True, True, False, False),
        (False, False, False, False),
        (False, True, True, False),
    ],
)
def test_should_heal_frr(unit_active, zebra_running, already_healed, expected):
    assert (
        should_heal_frr(
            unit_active=unit_active,
            zebra_running=zebra_running,
            already_healed=already_healed,
        )
        is expected
    )


class _FakeRuntime:
    """Models a router whose watchfrr is dead unless the heal revives it."""

    def __init__(self, *, zebra_running: bool, heal_revives: bool) -> None:
        self.zebra_running = zebra_running
        self.heal_revives = heal_revives
        self.unit_active = False
        self.heals = 0

    def exec(self, host: str, command: str, timeout: float = 10.0) -> str:
        if command == "systemctl is-active frr":
            return "active\n" if self.unit_active else "failed\n"
        if command == "pgrep -x zebra":
            return "55\n" if self.zebra_running else ""
        if command == "service frr start":
            self.heals += 1
            self.unit_active = self.heal_revives
            return ""
        raise AssertionError(f"unexpected command {command!r}")


def test_heal_revives_watchfrr_once():
    runtime = _FakeRuntime(zebra_running=True, heal_revives=True)
    healed: set[str] = set()
    assert frr_active_or_heal(runtime, "r1", healed)
    assert healed == {"r1"}
    assert frr_active_or_heal(runtime, "r1", healed)
    assert runtime.heals == 1


def test_failed_heal_is_not_retried_but_zebra_counts():
    runtime = _FakeRuntime(zebra_running=True, heal_revives=False)
    healed: set[str] = set()
    # Unit stays failed after heal; zebra still proves control-plane readiness.
    assert frr_active_or_heal(runtime, "r1", healed)
    assert frr_active_or_heal(runtime, "r1", healed)
    assert runtime.heals == 1


@pytest.mark.parametrize("healed", [set(), None])
def test_no_heal_without_zebra_or_heal_set(healed):
    runtime = _FakeRuntime(zebra_running=healed is None, heal_revives=True)
    assert not frr_active_or_heal(runtime, "r1", healed)
    assert runtime.heals == 0
