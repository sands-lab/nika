"""Docker E2E for p4_table_entry_missing/misconfig on p4_dc_fabric."""

from __future__ import annotations

import time

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.net_env.verify import http_ok
from nika.problems.registry import get_problem_class, list_avail_problem_names
from nika.net_env.net_env_pool import get_probe_path
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from nika.validation.symptom import evaluate_symptom, get_symptom_contract

SCENARIO = "p4_dc_fabric"
TOPO_SIZES = ("s", "m")


@pytest.mark.contract
def test_failures_register_with_path_http_contract() -> None:
    for problem in ("p4_table_entry_missing", "p4_table_entry_misconfig"):
        assert problem in list_avail_problem_names()
        contract = get_symptom_contract(problem)
        assert contract.symptom_class == "unreachable"
        assert contract.probe == "path_http"


@pytest.mark.e2e
@pytest.mark.parametrize(
    "problem", ("p4_table_entry_missing", "p4_table_entry_misconfig")
)
@pytest.mark.parametrize("topo_size", TOPO_SIZES)
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4TableEntryFabricE2ETest(IntegrationTestCase):
    def test_inject_verify_symptom_unreachable(
        self, problem: str, topo_size: str
    ) -> None:
        path = get_probe_path(SCENARIO, topo_size=topo_size)
        assert path is not None and path.http_url

        params = resolve_inject_params(problem, SCENARIO, topo_size, seed=0)
        assert params["host_name"] == "leaf_1"
        assert params.get("probe_dst_ip")
        assert params.get("observer_device")

        session_id = None
        try:
            session_id = self._start_env(SCENARIO, ["-s", topo_size])
            self._assert_session_ready(session_id, SCENARIO)

            cls = get_problem_class(problem)
            assert cls is not None
            instance = self._problem(cls, session_id=session_id)
            parsed = instance.parse_params(params)
            runtime = instance.runtime

            assert http_ok(runtime, path.src_host, path.http_url)

            from nika.validation.symptom.probe import _resolve_path, run_probe_snapshot

            symptom_path = _resolve_path(
                SCENARIO, parsed, topo_size=topo_size, net_env=instance.net_env
            )
            before = run_probe_snapshot(
                runtime, "path_http", symptom_path, params=parsed
            )
            assert before.http_ok is True
            instance.inject_fault(parsed)
            time.sleep(1)

            verify = instance.verify_fault(parsed)
            assert verify["verified"] is True, verify

            ok, symptom = evaluate_symptom(
                runtime,
                problem,
                parsed,
                scenario=SCENARIO,
                topo_size=topo_size,
                before=before,
                problem=instance,
            )
            assert ok is True, symptom
            assert symptom.get("after", {}).get("http_ok") is False, symptom
        finally:
            if session_id is not None:
                self._close_session(session_id)
