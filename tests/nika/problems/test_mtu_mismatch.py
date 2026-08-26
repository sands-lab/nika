"""Integration tests for mtu_mismatch (path MTU / Frag Needed symptoms)."""

from __future__ import annotations

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.problems.registry import get_problem_class
from nika.problems.support.probe_paths import get_probe_path
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestMtuMismatchDcClos(IntegrationTestCase):
    """Small DF ping succeeds; large DF fails with Frag Needed feedback."""

    SCENARIO = "dc_clos"
    PROBLEM = "mtu_mismatch"

    def test_inject_behavior_restore_cleanup(self) -> None:
        from nika.net_env.verify import ping_df_probe, ping_mtu_frag_needed

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

            path = get_probe_path(self.SCENARIO, topo_size="s")
            assert path is not None and path.dst_ip
            src = path.src_host
            dst = path.dst_ip

            try:
                problem.inject_fault(parsed)
                verify = problem.verify_fault(parsed)
                assert verify["verified"] is True, verify

                small_ok, _, _ = ping_df_probe(runtime, src, dst, packet_size=64)
                large_ok, _, _ = ping_df_probe(runtime, src, dst, packet_size=1400)
                assert small_ok is True
                assert large_ok is False
                assert ping_mtu_frag_needed(runtime, src, dst) is True

                recovered = problem.recover_fault(parsed)
                assert recovered["verified"] is True, recovered
            finally:
                if problem is not None and parsed is not None:
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
