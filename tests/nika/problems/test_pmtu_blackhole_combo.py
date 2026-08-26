"""Combo integration: mtu_mismatch + icmp_frag_needed_filter (PMTUD black hole)."""

from __future__ import annotations

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.problems.registry import get_problem_class
from nika.problems.support.probe_paths import get_probe_path
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available


@pytest.mark.skipif(not docker_available(), reason="docker required")
class TestPmtuBlackholeComboDcClos(IntegrationTestCase):
    """Lower path MTU then drop Frag Needed on the same router → silent large-DF fail."""

    SCENARIO = "dc_clos"
    MTU_PROBLEM = "mtu_mismatch"
    FILTER_PROBLEM = "icmp_frag_needed_filter_misconfiguration"

    def test_inject_behavior_restore_cleanup(self) -> None:
        from nika.net_env.verify import ping_df_probe, ping_mtu_blackhole

        session_id = None
        try:
            session_id = self._start_env(self.SCENARIO, ["-s", "s"])
            self._assert_session_ready(session_id, self.SCENARIO)

            mtu_params = resolve_inject_params(
                self.MTU_PROBLEM, self.SCENARIO, "s", seed=1
            )
            # Same router so Frag Needed is generated then dropped locally.
            filter_params = {"host_name": mtu_params["host_name"]}

            mtu_cls = get_problem_class(self.MTU_PROBLEM)
            filter_cls = get_problem_class(self.FILTER_PROBLEM)
            assert mtu_cls is not None and filter_cls is not None
            mtu_problem = self._problem(mtu_cls, session_id=session_id)
            filter_problem = self._problem(filter_cls, session_id=session_id)
            mtu_parsed = mtu_problem.parse_params(mtu_params)
            filter_parsed = filter_problem.parse_params(filter_params)
            runtime = mtu_problem.runtime

            path = get_probe_path(self.SCENARIO, topo_size="s")
            assert path is not None and path.dst_ip
            src = path.src_host
            dst = path.dst_ip

            try:
                mtu_problem.inject_fault(mtu_parsed)
                filter_problem.inject_fault(filter_parsed)

                mtu_verify = mtu_problem.verify_fault(mtu_parsed)
                filter_verify = filter_problem.verify_fault(filter_parsed)
                assert mtu_verify["verified"] is True, mtu_verify
                assert filter_verify["verified"] is True, filter_verify

                small_ok, _, _ = ping_df_probe(runtime, src, dst, packet_size=64)
                large_ok, saw_frag, _ = ping_df_probe(
                    runtime, src, dst, packet_size=1400
                )
                assert small_ok is True
                assert large_ok is False
                assert saw_frag is False
                assert ping_mtu_blackhole(runtime, src, dst) is True
            finally:
                for problem, parsed in (
                    (filter_problem, filter_parsed),
                    (mtu_problem, mtu_parsed),
                ):
                    try:
                        problem.recover_fault(parsed)
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            if session_id is not None:
                self._close_session(session_id)
