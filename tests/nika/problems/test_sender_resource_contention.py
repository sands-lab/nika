"""Unit and integration tests for sender_resource_contention."""

from __future__ import annotations

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.problems.ownership import owner_kind_for_fault
from nika.problems.registry import get_problem_class, list_avail_problem_names
from nika.problems.support.cpu_quota_helpers import cpu_quota_to_nano_cpus
from tests.support.symptom import evaluate_symptom, get_symptom_contract
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


def test_failure_registers_and_contracts() -> None:
    assert "sender_resource_contention" in list_avail_problem_names()
    cls = get_problem_class("sender_resource_contention")
    assert cls is not None
    assert owner_kind_for_fault("sender_resource_contention") == "node_or_k8s"
    contract = get_symptom_contract("sender_resource_contention")
    assert contract.symptom_class == "degradation"
    assert contract.probe == "custom"
    assert (
        "CPU" in (cls.symptom_desc or "") or "cpu" in (cls.symptom_desc or "").lower()
    )
    assert "evaluate_symptom" not in cls.__dict__


def test_inject_params_target_dc_clos_http_server() -> None:
    params = resolve_inject_params("sender_resource_contention", "dc_clos", "s", seed=1)
    assert params["host_name"] == "webserver0_pod0"
    assert params["client_host"] == "client_0"
    assert params["large_url"].endswith("/large.bin")
    assert params["small_url"].endswith("/small.bin")
    assert float(params["cpu_quota"]) == 0.05


def test_cpu_quota_to_nano_cpus() -> None:
    from nika.problems.support.cpu_quota_helpers import nano_cpus_to_cfs

    assert cpu_quota_to_nano_cpus(0.25) == 250_000_000
    assert cpu_quota_to_nano_cpus(1.0) == 1_000_000_000
    assert nano_cpus_to_cfs(250_000_000) == (100_000, 25_000)
    assert nano_cpus_to_cfs(0) == (100_000, -1)
    with pytest.raises(ValueError):
        cpu_quota_to_nano_cpus(0.0)


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestSenderResourceContentionDcClos(IntegrationTestCase):
    """Behavioral loop: CPU quota + stress slows large HTTP; path stays healthy."""

    SCENARIO = "dc_clos"
    PROBLEM = "sender_resource_contention"

    def test_inject_behavior_restore_cleanup(self) -> None:
        from nika.net_env.verify import (
            http_download_stats,
            median_throughput_bps,
            ping_stats,
        )
        from nika.problems.support.cpu_quota_helpers import read_nano_cpus

        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(self.SCENARIO, ["-s", "s"])
            self._assert_session_ready(session_id, self.SCENARIO)

            params = resolve_inject_params(self.PROBLEM, self.SCENARIO, "s", seed=1)
            cls = get_problem_class(self.PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(params)
            runtime = problem.runtime

            server = parsed.host_name
            client = parsed.client_host
            dst_ip = parsed.dst_ip
            small_url = parsed.small_url
            large_url = parsed.large_url

            small_ok = http_download_stats(runtime, client, small_url, max_time_sec=30)
            assert small_ok.ok, small_ok.raw

            healthy_rtt = ping_stats(
                runtime, client, dst_ip, count=10, interval_sec=0.2
            )
            assert healthy_rtt.loss_percent < 5.0
            assert healthy_rtt.rtt_avg_ms is not None

            original_nano = read_nano_cpus(runtime, server)
            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify
                artifact = verify["details"]
                assert artifact["stress_running"] is True
                assert artifact["quota_ok"] is True

                ok, symptom = evaluate_symptom(
                    runtime,
                    self.PROBLEM,
                    parsed,
                    scenario=self.SCENARIO,
                    problem=problem,
                )
                assert ok is True, symptom
                details = symptom["details"]
                assert details["path_ok"] is True
                assert details["perf_ok"] is True
                assert (
                    details["throughput_ratio"] is not None
                    or details["time_ratio"] is not None
                )
                if details["throughput_ratio"] is not None:
                    assert (
                        details["throughput_ratio"] <= 0.20
                        or (details["time_ratio"] or 0) >= 5.0
                    )

                fault_rtt = ping_stats(
                    runtime, client, dst_ip, count=10, interval_sec=0.2
                )
                assert fault_rtt.loss_percent < 5.0
                assert fault_rtt.rtt_avg_ms is not None
                assert fault_rtt.rtt_avg_ms <= healthy_rtt.rtt_avg_ms * 1.5

                small_fault = http_download_stats(
                    runtime, client, small_url, max_time_sec=30
                )
                assert small_fault.ok, small_fault.raw

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
                assert read_nano_cpus(runtime, server) == original_nano
                assert not runtime.process_running(server, "stress-ng")

                restored_bps = median_throughput_bps(
                    runtime, client, large_url, trials=3, max_time_sec=180
                )
                baseline_bps = details["baseline_throughput_bps"]
                assert restored_bps is not None and baseline_bps
                assert restored_bps >= 0.8 * baseline_bps, (
                    f"restored={restored_bps:.0f} baseline={baseline_bps:.0f}"
                )
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
