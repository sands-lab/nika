"""Combo integration: mtu_mismatch + icmp_frag_needed_filter (PMTUD black hole)."""

from __future__ import annotations

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_multi_inject_params
from nika.net_env.net_env_pool import get_probe_path
from nika.validation.presence import bound_injected_problem
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestPmtuBlackholeComboDcClos(IntegrationTestCase):
    """Lower path MTU then drop Frag Needed on the same router → silent large-DF fail."""

    SCENARIO = "dc_clos"
    PROBLEMS = ["mtu_mismatch", "icmp_frag_needed_filter_misconfiguration"]

    def test_inject_behavior_restore_cleanup(self) -> None:
        from nika.net_env.verify import ping_df_probe, ping_mtu_blackhole

        params = resolve_multi_inject_params(self.PROBLEMS, self.SCENARIO, "s", seed=42)
        session_id = None
        try:
            session_id = self._start_env(self.SCENARIO, ["-s", "s"])
            self._assert_session_ready(session_id, self.SCENARIO)

            self._inject_multi_failure(self.PROBLEMS, params, session_id=session_id)
            self._assert_multi_failure_injected(self.PROBLEMS, session_id=session_id)

            bound = bound_injected_problem(session_id)
            assert bound is not None
            runtime = bound[0].runtime

            path = get_probe_path(self.SCENARIO, topo_size="s")
            assert path is not None and path.dst_ip
            src = path.src_host
            dst = path.dst_ip

            small_ok, _, _ = ping_df_probe(runtime, src, dst, packet_size=64)
            large_ok, saw_frag, _ = ping_df_probe(runtime, src, dst, packet_size=1400)
            assert small_ok is True
            assert large_ok is False
            assert saw_frag is False
            assert ping_mtu_blackhole(runtime, src, dst) is True

            recovered = bound[0].recover_fault(bound[1])
            assert recovered["verified"], recovered
            assert bound[0].recover_fault(bound[1])["verified"]
            assert ping_df_probe(runtime, src, dst, packet_size=1400)[0]
        finally:
            if session_id is not None:
                self._close_session(session_id)
