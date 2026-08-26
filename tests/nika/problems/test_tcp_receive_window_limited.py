"""Unit and integration tests for tcp_receive_window_limited."""

from __future__ import annotations

import time

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.problems.ownership import owner_kind_for_fault
from nika.problems.registry import get_problem_class, list_avail_problem_names
from tests.support.symptom import get_symptom_contract
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


def test_failure_registers_and_contracts() -> None:
    assert "tcp_receive_window_limited" in list_avail_problem_names()
    cls = get_problem_class("tcp_receive_window_limited")
    assert cls is not None
    assert owner_kind_for_fault("tcp_receive_window_limited") == "node_or_k8s"
    contract = get_symptom_contract("tcp_receive_window_limited")
    assert contract.symptom_class == "degradation"
    assert contract.probe == "artifact_only"


def test_inject_params_target_http_client() -> None:
    params = resolve_inject_params(
        "tcp_receive_window_limited", "enterprise_branch", "m", seed=1
    )
    assert params["host_name"] == "br1_corp_pc"
    assert params["sender_host"] == "hq_srv"
    assert params["large_url"].endswith("/large.bin")


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestTcpReceiveWindowLimitedEnterprise(IntegrationTestCase):
    """inject → verify → recover on enterprise_branch (allowlisted scenario)."""

    SCENARIO = "enterprise_branch"
    PROBLEM = "tcp_receive_window_limited"

    def test_inject_behavior_restore_cleanup(self) -> None:
        from nika.net_env.verify import (
            http_download_stats,
            iperf_throughput_bps,
            median_throughput_bps,
            ping_stats,
        )
        from nika.problems.traffic_queueing_resource.tcp_rwnd_helpers import (
            primary_ipv4,
            read_sysctl_snapshot,
            sysctl_get,
        )

        session_id = None
        problem = None
        parsed = None
        try:
            session_id = self._start_env(self.SCENARIO, ["-s", "m"])
            self._assert_session_ready(session_id, self.SCENARIO)

            params = resolve_inject_params(self.PROBLEM, self.SCENARIO, "m", seed=1)
            params["baseline_trials"] = "1"
            params["iperf_duration_sec"] = "3"
            cls = get_problem_class(self.PROBLEM)
            assert cls is not None
            problem = self._problem(cls, session_id=session_id)
            parsed = problem.parse_params(params)
            runtime = problem.runtime

            receiver = parsed.host_name
            sender_host = parsed.sender_host
            sender_ip = parsed.sender_ip
            small_url = parsed.small_url
            large_url = parsed.large_url
            receiver_ip = primary_ipv4(runtime, receiver)
            assert receiver_ip

            small_ok = None
            for _ in range(30):
                small_ok = http_download_stats(
                    runtime, receiver, small_url, max_time_sec=30
                )
                if small_ok.ok:
                    break
                time.sleep(2)
            assert small_ok is not None and small_ok.ok, (
                small_ok.raw if small_ok else "no_probe"
            )

            healthy_rtt = ping_stats(
                runtime, receiver, sender_ip, count=5, interval_sec=0.2
            )
            assert healthy_rtt.rtt_avg_ms is not None
            assert healthy_rtt.rtt_avg_ms > 30.0
            assert healthy_rtt.loss_percent < 5.0

            healthy_bps = iperf_throughput_bps(
                runtime,
                sender_host,
                receiver,
                receiver_ip,
                duration_sec=3,
            )
            assert healthy_bps is not None and healthy_bps > 0

            healthy_http = median_throughput_bps(
                runtime,
                receiver,
                large_url,
                trials=1,
                max_time_sec=60,
                max_bytes=2 * 1024 * 1024,
            )
            assert healthy_http is not None and healthy_http > 0

            original = read_sysctl_snapshot(runtime, receiver)
            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify
                assert sysctl_get(
                    runtime, receiver, "net.ipv4.tcp_moderate_rcvbuf"
                ) == ("0")

                small_fault = http_download_stats(
                    runtime, receiver, small_url, max_time_sec=30
                )
                assert small_fault.ok, small_fault.raw
                fault_rtt = ping_stats(
                    runtime, receiver, sender_ip, count=5, interval_sec=0.2
                )
                assert fault_rtt.loss_percent < 5.0
                assert fault_rtt.rtt_avg_ms is not None
                assert fault_rtt.rtt_avg_ms < healthy_rtt.rtt_avg_ms * 1.5

                fault_bps = iperf_throughput_bps(
                    runtime,
                    sender_host,
                    receiver,
                    receiver_ip,
                    duration_sec=3,
                )
                assert fault_bps is not None
                assert fault_bps / healthy_bps < 0.5, (
                    f"iperf healthy={healthy_bps:.0f} fault={fault_bps:.0f}"
                )

                fault_http = median_throughput_bps(
                    runtime,
                    receiver,
                    large_url,
                    trials=1,
                    max_time_sec=60,
                    max_bytes=2 * 1024 * 1024,
                )
                assert fault_http is not None
                assert fault_http / healthy_http < 0.5, (
                    f"http healthy={healthy_http:.0f} fault={fault_http:.0f}"
                )

                target = verify["details"]["target_buffer_bytes"]
                rmem = sysctl_get(runtime, receiver, "net.ipv4.tcp_rmem")
                assert str(target) in rmem.replace("\t", " ")

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
                restored = read_sysctl_snapshot(runtime, receiver)
                assert restored.moderate_rcvbuf == original.moderate_rcvbuf

                restored_bps = iperf_throughput_bps(
                    runtime,
                    sender_host,
                    receiver,
                    receiver_ip,
                    duration_sec=3,
                )
                assert restored_bps is not None
                assert restored_bps / healthy_bps >= 0.8, (
                    f"restored={restored_bps:.0f} healthy={healthy_bps:.0f}"
                )
            finally:
                try:
                    problem.recover_fault(parsed)
                except Exception:  # noqa: BLE001
                    pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
