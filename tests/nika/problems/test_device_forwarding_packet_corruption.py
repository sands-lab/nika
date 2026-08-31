"""Docker E2E for device_forwarding_packet_corruption (TC BPF gray TCP degradation)."""

from __future__ import annotations

import time
from dataclasses import dataclass

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.net_env.verify import http_download_stats, iperf_tcp_metrics, ping_stats
from nika.problems.registry import get_problem_class, list_avail_problem_names
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.symptom import evaluate_symptom, get_symptom_contract
from tests.support.symptom.corruption_probes import _service_peer_host
from tests.support.symptom.flap_probes import assert_baseline_healthy
from tests.support.symptom.probe import _resolve_path

PROBLEM = "device_forwarding_packet_corruption"
SEEDS = (0, 1, 4, 7, 42)
IPERF_DURATION_SEC = 5
BASELINE_RETRIES = 3
BASELINE_RETRY_SLEEP_SEC = 5.0
POST_INJECT_SLEEP_SEC = 10.0


@dataclass(frozen=True)
class ForwardingCorruptionCase:
    id: str
    scenario: str
    env_args: tuple[str, ...] = ("-s", "s")
    topo_size: str = "s"


CORRUPTION_CASES = (
    ForwardingCorruptionCase("dc_clos", "dc_clos"),
    ForwardingCorruptionCase("campus_lan", "campus_lan"),
    ForwardingCorruptionCase("enterprise_branch", "enterprise_branch"),
    ForwardingCorruptionCase("sdn_l3_clos", "sdn_l3_clos"),
)


def test_failure_registers_and_contracts() -> None:
    assert PROBLEM in list_avail_problem_names()
    cls = get_problem_class(PROBLEM)
    assert cls is not None
    contract = get_symptom_contract(PROBLEM)
    assert contract.symptom_class == "gray"
    assert contract.probe == "custom"
    assert "evaluate_symptom" not in cls.__dict__


def _skip_reason() -> str | None:
    if not docker_available():
        return "docker required"
    return None


def _endpoint_qdisc_clean(runtime, node: str, intf: str = "eth0") -> bool:
    output = runtime.exec(
        node, f"tc qdisc show dev {intf} 2>/dev/null || true"
    ).lower()
    return "netem" not in output and "tbf" not in output


def _assert_baseline_with_retry(runtime, path) -> tuple[bool, dict]:
    last: dict = {}
    for attempt in range(BASELINE_RETRIES):
        ok, details = assert_baseline_healthy(runtime, path)
        last = details
        if ok:
            return True, details
        if attempt + 1 < BASELINE_RETRIES:
            time.sleep(BASELINE_RETRY_SLEEP_SEC)
    return False, last


def _capture_degradation_baselines(problem, runtime, path, *, scenario: str) -> None:
    from tests.support.symptom.corruption_probes import _service_peer_host

    peer_host = _service_peer_host(problem, path, scenario)
    baseline_bps, baseline_retrans = iperf_tcp_metrics(
        runtime,
        path.src_host,
        peer_host,
        path.dst_ip,
        duration_sec=IPERF_DURATION_SEC,
        port=15241,
    )
    if baseline_bps is not None:
        problem._baseline_iperf_bps = baseline_bps
        problem._baseline_iperf_retrans = baseline_retrans or 0
    if path.http_url:
        http = http_download_stats(
            runtime,
            path.src_host,
            path.http_url,
            max_time_sec=30,
            connect_timeout_sec=5,
        )
        if http.ok and http.time_total_s is not None:
            problem._baseline_http_time_s = http.time_total_s
    ping = ping_stats(runtime, path.src_host, path.dst_ip, count=15, interval_sec=0.2)
    if ping.rtt_avg_ms is not None:
        problem._baseline_rtt_ms = ping.rtt_avg_ms


@pytest.mark.parametrize("seed", SEEDS)
@pytest.mark.parametrize("case", CORRUPTION_CASES, ids=[c.id for c in CORRUPTION_CASES])
class TestDeviceForwardingPacketCorruptionE2E(IntegrationTestCase):
    """Inject → verify_fault → gray TCP symptom → recover."""

    def test_inject_verify_symptom_recover(
        self, case: ForwardingCorruptionCase, seed: int
    ) -> None:
        skip = _skip_reason()
        if skip:
            pytest.skip(skip)

        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(case.scenario, list(case.env_args))
            self._assert_session_ready(session_id, case.scenario)

            raw = resolve_inject_params(
                PROBLEM,
                case.scenario,
                case.topo_size,
                seed=seed,
            )
            cls = get_problem_class(PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(raw)
            runtime = problem.runtime

            path = _resolve_path(case.scenario, parsed, topo_size=case.topo_size)
            assert path is not None and path.dst_ip and path.peer_host

            baseline_ok, baseline = _assert_baseline_with_retry(runtime, path)
            assert baseline_ok is True, baseline

            _capture_degradation_baselines(problem, runtime, path, scenario=case.scenario)
            assert (
                getattr(problem, "_baseline_iperf_bps", None) is not None
                or getattr(problem, "_baseline_http_time_s", None) is not None
            ), "need at least one degradation baseline metric"

            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify

                time.sleep(POST_INJECT_SLEEP_SEC)

                tc_out = runtime.exec(
                    parsed.forwarding_device,
                    f"tc filter show dev {parsed.intf_name} egress 2>/dev/null || true",
                ).lower()
                assert "bpf" in tc_out, tc_out

                assert _endpoint_qdisc_clean(runtime, path.src_host)
                if path.peer_host and path.peer_host != path.src_host:
                    assert _endpoint_qdisc_clean(runtime, path.peer_host)

                ok, symptom = evaluate_symptom(
                    runtime,
                    PROBLEM,
                    parsed,
                    scenario=case.scenario,
                    topo_size=case.topo_size,
                    problem=problem,
                )
                assert ok is True, symptom
                cmp = symptom.get("comparison", {})
                assert cmp.get("artifact_attached") is True, symptom
                assert cmp.get("tcp_degraded") is True, symptom
                assert cmp.get("path_reachable") is True, symptom
                assert cmp.get("no_shortcut") is True, symptom

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered

                baseline_bps = getattr(problem, "_baseline_iperf_bps", None)
                if baseline_bps is not None:
                    restored_bps, _ = iperf_tcp_metrics(
                        runtime,
                        path.src_host,
                        _service_peer_host(problem, path, case.scenario),
                        path.dst_ip,
                        duration_sec=IPERF_DURATION_SEC,
                        port=15242,
                    )
                    assert restored_bps is not None
                    assert float(restored_bps) >= float(baseline_bps) * 0.7
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
