"""SYN pressure includes measured exhaustion of small listener backlogs."""

import pytest

from nika.problems.security.tcp_syn_flood import (
    parse_syn_queue_read,
    syn_queue_pressure,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("half_open", "backlog", "cookies", "pressure"),
    [
        (6, 5, 10, True),
        (6, 5, 0, False),
        (6, 20, 10, False),
        (10, 128, 0, True),
        (0, 0, 10, False),
    ],
)
def test_pressure_requires_many_handshakes_or_measured_backlog_exhaustion(
    half_open, backlog, cookies, pressure
):
    assert syn_queue_pressure(half_open, backlog, cookies) is pressure


def test_queue_read_uses_counter_delta_and_requires_live_capacity():
    output = "listener_backlog 5\ncookies_before 100\ncookies_after 170\nhalf_open 6\n"
    assert parse_syn_queue_read(output) == {
        "listener_backlog": 5,
        "half_open": 6,
        "cookies_delta": 70,
    }
    with pytest.raises(KeyError):
        parse_syn_queue_read("half_open 6\n")


@pytest.mark.parametrize("count", [0, 100])
def test_artifact_count_keeps_zero_distinct_from_observation_errors(count):
    from types import SimpleNamespace
    from nika.problems.security.tcp_syn_flood import (
        TcpSynFloodAttack,
        TcpSynFloodAttackParams,
    )
    import time

    calls = []

    def execute(node, command, **kwargs):
        calls.append((node, command, kwargs))
        if node == "client_1":
            return f"{count}\n__rc=0\n"
        return "yes" if "pgrep" in command else str(time.time())

    problem = TcpSynFloodAttack()
    problem.runtime = SimpleNamespace(exec=execute)
    result = problem.verify_fault(
        TcpSynFloodAttackParams(attacker_device="client_1", target_ip="192.0.2.1")
    )
    assert result["verified"] is (count > 0)
    assert result["details"]["flows_running"] == count


@pytest.mark.parametrize("raw", ["[TIMEOUT] exceeded 60s", "pgrep failed\n__rc=2"])
def test_artifact_count_propagates_observation_failure(raw):
    from types import SimpleNamespace
    from nika.problems.security.tcp_syn_flood import (
        TcpSynFloodAttack,
        TcpSynFloodAttackParams,
    )

    problem = TcpSynFloodAttack()
    problem.runtime = SimpleNamespace(exec=lambda *a, **kw: raw)
    with pytest.raises(RuntimeError, match="Observation on client_1"):
        problem.verify_fault(
            TcpSynFloodAttackParams(attacker_device="client_1", target_ip="192.0.2.1")
        )
