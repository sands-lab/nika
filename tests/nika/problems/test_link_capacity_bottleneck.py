"""Integration tests for link_capacity_bottleneck (VDE proxy TBF + iperf symptom)."""

from __future__ import annotations

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.problems.link_interface.link import _resolve_link_intf
from nika.problems.registry import get_problem_class
from nika.problems.support.probe_paths import get_probe_path
from nika.runtime.kathara.vde_proxy import KatharaVdeFaultProxy
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

IPERF_DURATION_SEC = 5
# Absolute floor used by evaluate_symptom for iperf_throughput.
LOW_BPS_MAX = 100_000.0


def _qdisc(runtime, node: str, intf: str) -> str:
    return runtime.exec(node, f"tc qdisc show dev {intf} 2>/dev/null || true").lower()


def _link_peer(runtime, host: str, intf: str) -> tuple[str, str] | None:
    """Return the other endpoint (node, intf) of the VDE proxy link, if present."""
    controller = KatharaVdeFaultProxy(runtime)
    state = controller.discover(host, intf)
    if state is None:
        return None
    if state.endpoint.node == host and state.endpoint.intf == intf:
        return state.peer.node, state.peer.intf
    return state.endpoint.node, state.endpoint.intf


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestLinkCapacityBottleneckDcClos(IntegrationTestCase):
    """Hidden controller TBF; endpoints leak no tbf/netem; iperf collapses; ping lives."""

    SCENARIO = "dc_clos"
    PROBLEM = "link_capacity_bottleneck"

    def test_inject_behavior_restore_cleanup(self) -> None:
        from nika.net_env.verify import iperf_throughput_bps, ping_stats

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
            assert problem.lab_backend == "kathara", problem.lab_backend

            path = get_probe_path(self.SCENARIO, topo_size="s")
            assert path is not None and path.peer_host and path.dst_ip
            src = path.src_host
            peer = path.peer_host
            peer_ip = path.dst_ip

            baseline_bps = iperf_throughput_bps(
                runtime, src, peer, peer_ip, duration_sec=IPERF_DURATION_SEC, port=15211
            )
            assert baseline_bps is not None and baseline_bps > LOW_BPS_MAX, baseline_bps

            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify

                resolved_intf = _resolve_link_intf(parsed.intf_name, "kathara")
                host_qdisc = _qdisc(runtime, parsed.host_name, resolved_intf)
                assert "tbf" not in host_qdisc and "netem" not in host_qdisc, host_qdisc

                peer_ep = _link_peer(runtime, parsed.host_name, resolved_intf)
                assert peer_ep is not None
                peer_qdisc = _qdisc(runtime, peer_ep[0], peer_ep[1])
                assert "tbf" not in peer_qdisc and "netem" not in peer_qdisc, peer_qdisc

                controller = KatharaVdeFaultProxy(runtime)
                proxy = controller.discover(parsed.host_name, resolved_intf)
                assert proxy is not None
                assert controller.tbf_configured(proxy)

                injected_bps = iperf_throughput_bps(
                    runtime,
                    src,
                    peer,
                    peer_ip,
                    duration_sec=IPERF_DURATION_SEC,
                    port=15212,
                )
                assert injected_bps is not None
                assert float(injected_bps) < LOW_BPS_MAX or (
                    float(injected_bps) < float(baseline_bps) * 0.5
                ), (baseline_bps, injected_bps)

                after_ping = ping_stats(
                    runtime, src, peer_ip, count=5, interval_sec=0.2
                )
                assert after_ping.received > 0

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered

                restored_bps = iperf_throughput_bps(
                    runtime,
                    src,
                    peer,
                    peer_ip,
                    duration_sec=IPERF_DURATION_SEC,
                    port=15213,
                )
                assert restored_bps is not None and float(restored_bps) > LOW_BPS_MAX
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
