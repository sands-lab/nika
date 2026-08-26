"""Live inject+verify for p4_dc_fabric existing and P4Runtime failures."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.net_env.p4_dc_fabric.verify import verify_p4_dc_fabric_lab
from nika.net_env.verify import http_ok, ping_ok
from nika.problems.prob_pool import get_problem_instance
from nika.runtime.factory import runtime_for_session
from nika.workflows.failure.inject import inject_failure as inject_failure_workflow
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

SCENARIO = "p4_dc_fabric"
TOPO_SIZE = "s"
REPORT_PATH = Path("results/test/p4_dc_fabric_failure_compat.json")

SAMPLED_EXISTING = (
    "bmv2_switch_down",
    "link_down",
    "link_flap",
    "link_packet_corruption",
    "link_bandwidth_throttling",
    "host_incorrect_ip",
    "host_missing_ip",
    "p4_table_entry_missing",
    "p4_table_entry_misconfig",
)
NEW_FAILURES = (
    "p4_action_selector_member_misconfig",
    "p4_ecmp_group_member_missing",
    "p4runtime_pipeline_mismatch",
    "p4runtime_partial_write",
    "p4_table_resource_exhaustion",
)
P4RUNTIME_FAILURES = (
    "p4runtime_pipeline_mismatch",
    "p4runtime_partial_write",
    "p4_table_entry_missing",
    "p4_table_entry_misconfig",
    "p4_action_selector_member_misconfig",
    "p4_ecmp_group_member_missing",
    "p4_table_resource_exhaustion",
)
P4RUNTIME_REPORT_PATH = Path("results/test/p4_dc_fabric_p4runtime_failures.json")


def _smoke(runtime, model) -> dict[str, bool]:
    src = model.client_endpoints()[0]
    dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)
    same = model.web_endpoints()[0]
    return {
        "cross_rack_ping": ping_ok(runtime, src.name, dst.ip),
        "cross_rack_http": http_ok(runtime, src.name, f"http://{dst.ip}/"),
        "same_rack_ping": ping_ok(runtime, src.name, same.ip)
        if src.leaf_id == same.leaf_id
        else ping_ok(runtime, src.name, model.web_endpoints()[src.leaf_id - 1].ip),
    }


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4DcFabricFailureCompatTest(IntegrationTestCase):
    def test_sampled_failures_inject_and_verify(self) -> None:
        problems = list(SAMPLED_EXISTING) + list(NEW_FAILURES)
        results: list[dict[str, Any]] = []
        for problem in problems:
            row: dict[str, Any] = {
                "failure": problem,
                "injection_success": False,
                "verify_fault": False,
                "regression_status": "fail",
                "error": None,
            }
            session_id: str | None = None
            try:
                params = resolve_inject_params(
                    problem, SCENARIO, topo_size=TOPO_SIZE, seed=0
                )
                row["params"] = params
                session_id = self._start_env(SCENARIO, ["-s", TOPO_SIZE])
                meta = self._assert_session_ready(session_id, SCENARIO)
                runtime = runtime_for_session(meta)
                model = build_clos_fabric_model(TOPO_SIZE)
                healthy = verify_p4_dc_fabric_lab(
                    runtime, scenario_name=SCENARIO, model=model
                )
                row["healthy_before"] = bool(healthy.get("verified"))
                smoke_before = _smoke(runtime, model)
                inject_failure_workflow(
                    [problem], session_id=session_id, param_overrides=params
                )
                row["injection_success"] = True
                time.sleep(2)
                problem_obj = get_problem_instance(
                    [problem],
                    scenario_name=SCENARIO,
                    **self._scenario_kwargs(session_id),
                )
                verify = problem_obj.verify_fault(params=problem_obj.Params(**params))
                verify_ok = bool(verify.get("verified"))
                row["verify_fault"] = verify_ok
                row["smoke_before"] = smoke_before
                row["smoke_after"] = _smoke(runtime, model)
                row["regression_status"] = (
                    "pass" if row["injection_success"] and verify_ok else "fail"
                )
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"{type(exc).__name__}: {exc}"
            finally:
                if session_id is not None:
                    try:
                        self._close_session(session_id)
                    except Exception as close_exc:  # noqa: BLE001
                        row["error"] = (
                            f"{row.get('error') or ''}; close: {close_exc}"
                        ).strip("; ")
                results.append(row)
                REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
                REPORT_PATH.write_text(
                    json.dumps({"results": results}, indent=2, default=str),
                    encoding="utf-8",
                )
        failed = [r for r in results if r["regression_status"] != "pass"]
        assert not failed, json.dumps(failed, indent=2, default=str)


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class P4DcFabricP4RuntimeFailureLiveTest(IntegrationTestCase):
    def test_p4runtime_failures_inject_verify_restore(self) -> None:
        from nika.net_env.p4_dc_fabric.fabric_manager.apply import (
            reconcile_fabric,
        )

        session_id = self._start_env(SCENARIO, ["-s", TOPO_SIZE])
        results: list[dict[str, Any]] = []
        try:
            meta = self._assert_session_ready(session_id, SCENARIO)
            runtime = runtime_for_session(meta)
            model = build_clos_fabric_model(TOPO_SIZE)
            healthy = verify_p4_dc_fabric_lab(
                runtime, scenario_name=SCENARIO, model=model
            )
            assert healthy.get("verified"), healthy
            for problem in P4RUNTIME_FAILURES:
                row: dict[str, Any] = {
                    "failure": problem,
                    "injection_success": False,
                    "verify_fault": False,
                    "regression_status": "fail",
                    "error": None,
                }
                try:
                    params = resolve_inject_params(
                        problem, SCENARIO, topo_size=TOPO_SIZE, seed=0
                    )
                    params["host_name"] = "leaf_1"
                    row["params"] = params
                    inject_failure_workflow(
                        [problem], session_id=session_id, param_overrides=params
                    )
                    row["injection_success"] = True
                    problem_obj = get_problem_instance(
                        [problem],
                        scenario_name=SCENARIO,
                        **self._scenario_kwargs(session_id),
                    )
                    verify = problem_obj.verify_fault(
                        params=problem_obj.Params(**params)
                    )
                    row["verify_fault"] = bool(verify.get("verified"))
                    row["verify_details"] = verify.get("details")
                    row["smoke_after"] = _smoke(runtime, model)
                    reconcile_fabric(runtime, model)
                    restored = _smoke(runtime, model)
                    row["restored"] = restored
                    row["regression_status"] = (
                        "pass"
                        if row["injection_success"]
                        and row["verify_fault"]
                        and restored.get("cross_rack_ping")
                        else "fail"
                    )
                except Exception as exc:  # noqa: BLE001
                    row["error"] = f"{type(exc).__name__}: {exc}"
                    try:
                        reconcile_fabric(runtime, model)
                    except Exception as restore_exc:  # noqa: BLE001
                        row["error"] += f"; restore: {restore_exc}"
                results.append(row)
                P4RUNTIME_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
                P4RUNTIME_REPORT_PATH.write_text(
                    json.dumps({"results": results}, indent=2, default=str),
                    encoding="utf-8",
                )
        finally:
            self._close_session(session_id)
        failed = [r for r in results if r["regression_status"] != "pass"]
        assert not failed, json.dumps(failed, indent=2, default=str)
