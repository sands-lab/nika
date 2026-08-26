"""Live inject, verify, and restore tests for shared P4Runtime failures."""

from __future__ import annotations

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.net_env.p4_dc_gateway.apply import reconcile_gateway
from nika.net_env.p4_dc_gateway.topology_model import (
    build_gateway_fabric_model,
)
from nika.net_env.verify import http_ok
from nika.problems.prob_pool import get_problem_instance
from nika.runtime.factory import runtime_for_session
from nika.workflows.failure.inject import inject_failure as inject_failure_workflow
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

SCENARIO = "p4_dc_gateway"
TOPO_SIZE = "s"
P4RUNTIME_FAILURES = (
    "p4_table_entry_missing",
    "p4_table_entry_misconfig",
    "p4_action_selector_member_misconfig",
    "p4_ecmp_group_member_missing",
    "p4runtime_pipeline_mismatch",
    "p4runtime_partial_write",
    "p4_table_resource_exhaustion",
)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4DcGatewayP4RuntimeFailureCompatTest(IntegrationTestCase):
    def test_shared_failures_inject_verify_restore(self) -> None:
        session_id = self._start_env(SCENARIO, ["-s", TOPO_SIZE])
        try:
            meta = self._assert_session_ready(session_id, SCENARIO)
            runtime = runtime_for_session(meta)
            model = build_gateway_fabric_model(TOPO_SIZE)
            source = model.clients[0]
            url = model.web_urls[0]
            assert http_ok(runtime, source.name, url)

            for problem in P4RUNTIME_FAILURES:
                params = resolve_inject_params(
                    problem, SCENARIO, topo_size=TOPO_SIZE, seed=0
                )
                params["host_name"] = "gateway_1"
                inject_failure_workflow(
                    [problem], session_id=session_id, param_overrides=params
                )
                instance = get_problem_instance(
                    [problem],
                    scenario_name=SCENARIO,
                    **self._scenario_kwargs(session_id),
                )
                verified = instance.verify_fault(params=instance.Params(**params))
                assert verified.get("verified"), (problem, verified)
                reconcile_gateway(runtime, model)
                assert http_ok(runtime, source.name, url), problem
        finally:
            self._close_session(session_id)
