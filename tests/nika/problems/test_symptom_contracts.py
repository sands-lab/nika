"""Contract tests for test-path failure symptom metadata."""

from nika.problems.registry import list_avail_problem_names
from tests.support.symptom.types import ProbePath
from tests.support.symptom import (
    get_symptom_contract,
    list_symptom_contracts,
)
from tests.support.symptom.custom import _CUSTOM


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


def test_gray_probe_bounds() -> None:
    from tests.support.symptom.gray_probes import probe_gray_packet_loss

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
