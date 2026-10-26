"""Contract tests for test-path failure symptom metadata."""

import pytest

from nika.problems.registry import list_avail_problem_names
from nika.validation.symptom.types import ProbePath
from nika.validation.symptom import (
    get_symptom_contract,
    list_symptom_contracts,
)
from nika.validation.symptom.custom import _CUSTOM, _CUSTOM_BASELINE

pytestmark = pytest.mark.contract


def test_all_failures_have_symptom_contracts() -> None:
    registered = set(list_avail_problem_names())
    contracted = {c.failure for c in list_symptom_contracts()}
    missing = registered - contracted
    assert not missing, f"missing symptom contracts: {sorted(missing)}"


def test_symptom_contract_fields() -> None:
    for failure in list_avail_problem_names():
        contract = get_symptom_contract(failure)
        assert contract.failure == failure
        assert contract.probe
        if contract.probe == "custom":
            assert failure in _CUSTOM, f"custom probe missing handler: {failure}"


# Custom symptoms that the audit cannot admit without a same-metric healthy
# baseline: BLOCKED faults whose frozen release cases cannot be made valid.
# Do not add new entries; add a baseline instead.
_CUSTOM_WITHOUT_BASELINE: frozenset[str] = frozenset()


def test_custom_symptoms_declare_a_healthy_baseline() -> None:
    custom = {c.failure for c in list_symptom_contracts() if c.probe == "custom"}
    missing = {f for f in custom if f not in _CUSTOM_BASELINE}
    unexpected = missing - _CUSTOM_WITHOUT_BASELINE
    assert not unexpected, f"custom symptoms without a baseline: {sorted(unexpected)}"
    unknown = _CUSTOM_WITHOUT_BASELINE - custom
    assert not unknown, f"exceptions that are not custom symptoms: {sorted(unknown)}"


def test_gray_probe_bounds() -> None:
    from nika.validation.symptom.gray_probes import probe_gray_packet_loss

    class _Runtime:
        def exec(self, host, cmd, timeout=10.0):
            return (
                "100 packets transmitted, 97 received, 3.0% packet loss, time 99000ms\n"
                "rtt min/avg/max/mdev = 0.1/0.2/0.3/0.1 ms"
            )

    ok, details = probe_gray_packet_loss(
        _Runtime(),
        ProbePath(src_host="pc1", dst_ip="10.0.0.2"),
    )
    assert ok
    assert details["loss_percent"] == 3.0


def test_degradation_does_not_accept_an_already_slow_baseline() -> None:
    from nika.net_env.verify import compare_symptom

    ok, details = compare_symptom(
        {"http_ok": True, "http_time_ms": 839.505},
        {"http_ok": True, "http_time_ms": 840.446},
        "degraded",
    )

    assert not ok
    assert details["observed"]["absolute_slow"] is False


def test_parse_ping_stats_loss() -> None:
    from nika.net_env.verify import _parse_ping_stats

    output = (
        "20 packets transmitted, 10 received, 50% packet loss, time 19000ms\n"
        "rtt min/avg/max/mdev = 0.045/0.052/0.061/0.004 ms"
    )
    stats = _parse_ping_stats(output, count=20)
    assert stats.received == 10
    assert stats.loss_percent == 50.0
    assert stats.rtt_avg_ms == 0.052


def test_compare_symptom_unreachable() -> None:
    from nika.net_env.verify import compare_symptom

    ok, _ = compare_symptom(
        {"ping_ok": True},
        {"ping_ok": False},
        "unreachable",
    )
    assert ok


def test_compare_symptom_gray_loss() -> None:
    from nika.net_env.verify import compare_symptom

    ok, _ = compare_symptom(
        {"loss_percent": 0.0},
        {"loss_percent": 2.5},
        "gray_loss",
    )
    assert ok


def test_compare_symptom_unreachable_mtu_blackhole() -> None:
    from nika.net_env.verify import compare_symptom

    ok, details = compare_symptom(
        {"ping_ok": True, "mtu_blackhole": False},
        {"ping_ok": True, "mtu_blackhole": True},
        "unreachable",
    )
    assert ok
    assert details["observed"]["mtu_blackhole"] is True


def test_compare_symptom_rejects_a_signal_already_present_before_inject() -> None:
    from nika.net_env.verify import compare_symptom

    # The healthy path already returned frag-needed, so inject changed nothing.
    ok, _ = compare_symptom(
        {"ping_ok": True, "mtu_frag_needed": True},
        {"ping_ok": True, "mtu_frag_needed": True},
        "unreachable",
    )
    assert not ok


def test_compare_symptom_needs_a_measured_control_plane() -> None:
    from nika.net_env.verify import compare_symptom

    unmeasured = {"ping_ok": True, "control_plane_ok": None}
    assert not compare_symptom(unmeasured, unmeasured, "control_plane_down")[0]
    assert compare_symptom(
        {"control_plane_ok": True}, {"control_plane_ok": False}, "control_plane_down"
    )[0]


def test_compare_symptom_degraded_rtt() -> None:
    from nika.net_env.verify import compare_symptom

    ok, details = compare_symptom(
        {"rtt_avg_ms": 1.0, "http_time_ms": None},
        {"rtt_avg_ms": 20.0, "http_time_ms": None},
        "degraded",
        latency_factor=2.0,
    )
    assert ok
    assert details["observed"]["slower_rtt"] is True


def test_compare_symptom_degraded_loss() -> None:
    from nika.net_env.verify import compare_symptom

    ok, details = compare_symptom(
        {"loss_percent": 0.0, "ping_ok": True},
        {"loss_percent": 65.0, "ping_ok": True, "rtt_avg_ms": 4120.0},
        "degraded",
        loss_min_percent=10.0,
    )
    assert ok
    assert details["observed"]["loss_degraded"] is True
    # Without a healthy loss reading there is nothing to compare against.
    unmeasured, _ = compare_symptom(
        {"loss_percent": None, "ping_ok": None},
        {"loss_percent": 65.0, "ping_ok": True},
        "degraded",
    )
    assert not unmeasured


def test_compare_symptom_degraded_absolute_http() -> None:
    from nika.net_env.verify import compare_symptom

    ok, _ = compare_symptom(
        {"http_time_ms": 5.0},
        {"http_time_ms": 800.0},
        "degraded",
    )
    assert ok


def test_compare_symptom_isolation_needs_a_working_control() -> None:
    from nika.net_env.verify import compare_symptom

    # Isolation means the target broke while the control path kept working.
    ok, details = compare_symptom(
        {"symptom_ok": True},
        {"symptom_ok": False},
        "isolation",
    )
    assert not ok
    assert details["observed"]["control_intact"] is False
    assert compare_symptom(
        {"symptom_ok": True},
        {"symptom_ok": False, "control_ok": True},
        "isolation",
    )[0]
    # Without a healthy reading of the target, the break is not shown.
    assert not compare_symptom(
        {}, {"symptom_ok": False, "control_ok": True}, "isolation"
    )[0]
