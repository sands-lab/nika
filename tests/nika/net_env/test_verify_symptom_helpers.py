"""Unit tests for net_env verify symptom helpers."""

from nika.net_env.verify import _parse_ping_stats, compare_symptom


def test_parse_ping_stats_loss() -> None:
    output = (
        "20 packets transmitted, 10 received, 50% packet loss, time 19000ms\n"
        "rtt min/avg/max/mdev = 0.045/0.052/0.061/0.004 ms"
    )
    stats = _parse_ping_stats(output, count=20)
    assert stats.received == 10
    assert stats.loss_percent == 50.0
    assert stats.rtt_avg_ms == 0.052


def test_compare_symptom_unreachable() -> None:
    ok, _ = compare_symptom(
        {"ping_ok": True},
        {"ping_ok": False},
        "unreachable",
    )
    assert ok


def test_compare_symptom_gray_loss() -> None:
    ok, _ = compare_symptom(
        {"loss_percent": 0.0},
        {"loss_percent": 2.5},
        "gray_loss",
    )
    assert ok


def test_compare_symptom_unreachable_mtu_blackhole() -> None:
    ok, details = compare_symptom(
        {"ping_ok": True},
        {"ping_ok": True, "mtu_blackhole": True},
        "unreachable",
    )
    assert ok
    assert details["observed"]["mtu_blackhole"] is True


def test_compare_symptom_degraded_rtt() -> None:
    ok, details = compare_symptom(
        {"rtt_avg_ms": 1.0, "http_time_ms": None},
        {"rtt_avg_ms": 20.0, "http_time_ms": None},
        "degraded",
        latency_factor=2.0,
    )
    assert ok
    assert details["observed"]["slower_rtt"] is True


def test_compare_symptom_degraded_absolute_http() -> None:
    ok, _ = compare_symptom(
        {"http_time_ms": 5.0},
        {"http_time_ms": 800.0},
        "degraded",
    )
    assert ok
