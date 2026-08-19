"""Live compatibility: TAGS-compatible failures on sdn_l3_clos (inject + verify)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from benchmark.inject_resolve import resolve_inject_params
from nika.net_env.kathara.sdn.topology_model import build_clos_fabric_model
from nika.net_env.kathara.sdn.verify import verify_sdn_l3_clos_lab
from nika.net_env.verify import http_ok, ping_ok
from nika.problems.prob_pool import get_problem_instance, list_avail_problem_names
from nika.runtime.factory import runtime_for_session
from nika.workflows.benchmark.compatibility import compatible
from nika.workflows.failure.inject import inject_failure as inject_failure_workflow
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available

SCENARIO = "sdn_l3_clos"
TOPO_SIZE = "s"
REPORT_PATH = Path("results/test/sdn_l3_clos_failure_compat.json")

# Controller-plane faults may leave proactive dataplane forwarding intact.
DATAPLANE_MAY_STAY_UP = frozenset(
    {
        "sdn_controller_crash",
        "southbound_port_block",
        "southbound_port_mismatch",
    }
)


def compatible_sdn_failures() -> list[str]:
    return sorted(
        name for name in list_avail_problem_names() if compatible(name, SCENARIO)
    )


def _smoke(runtime, model) -> dict[str, bool]:
    src = model.client_endpoints()[0]
    dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)
    return {
        "cross_rack_ping": ping_ok(runtime, src.name, dst.ip),
        "cross_rack_http": http_ok(runtime, src.name, f"http://{dst.ip}/"),
    }


def _observable(
    problem: str,
    *,
    verify_ok: bool,
    smoke_before: dict[str, bool],
    smoke_after: dict[str, bool],
) -> bool:
    if verify_ok and problem in DATAPLANE_MAY_STAY_UP:
        # Injected control-plane evidence is enough; traffic often still works.
        return True
    if verify_ok and (
        smoke_before.get("cross_rack_ping") and not smoke_after.get("cross_rack_ping")
    ):
        return True
    if verify_ok and (
        smoke_before.get("cross_rack_http") and not smoke_after.get("cross_rack_http")
    ):
        return True
    # verify_fault itself is the observability contract for many host/ACL faults
    # that may not hit the sparse cross-rack smoke pair.
    return verify_ok


@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class SDNL3ClosFailureCompatTest(IntegrationTestCase):
    """One fresh lab per failure; write an aggregate JSON report."""

    def test_compatible_failures_inject_and_verify(self) -> None:
        problems = compatible_sdn_failures()
        assert problems, "expected TAGS-compatible failures for sdn_l3_clos"
        results: list[dict[str, Any]] = []

        for problem in problems:
            row: dict[str, Any] = {
                "failure": problem,
                "applicable": True,
                "params": {},
                "healthy_before": False,
                "injection_success": False,
                "verify_fault": False,
                "failure_observable": False,
                "recovery_success": None,
                "regression_status": "fail",
                "error": None,
                "smoke_before": {},
                "smoke_after": {},
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

                healthy = verify_sdn_l3_clos_lab(
                    runtime, scenario_name=SCENARIO, model=model
                )
                row["healthy_before"] = bool(healthy.get("verified"))
                smoke_before = _smoke(runtime, model)
                row["smoke_before"] = smoke_before
                if not row["healthy_before"]:
                    row["error"] = f"healthy verify failed: {healthy.get('checks')}"
                    row["regression_status"] = "fail"
                    results.append(row)
                    continue

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
                fault_params = problem_obj.Params(**params)
                verify = problem_obj.verify_fault(params=fault_params)
                verify_ok = bool(verify.get("verified"))
                row["verify_fault"] = verify_ok
                row["verify_details"] = {
                    k: verify.get(k) for k in ("verified", "fault_type", "details")
                }

                smoke_after = _smoke(runtime, model)
                row["smoke_after"] = smoke_after
                row["failure_observable"] = _observable(
                    problem,
                    verify_ok=verify_ok,
                    smoke_before=smoke_before,
                    smoke_after=smoke_after,
                )
                # Fresh lab per case is the recovery path for this matrix.
                row["recovery_success"] = True
                row["regression_status"] = (
                    "pass"
                    if row["injection_success"]
                    and row["verify_fault"]
                    and row["failure_observable"]
                    else "fail"
                )
            except Exception as exc:  # noqa: BLE001
                row["error"] = f"{type(exc).__name__}: {exc}"
                row["regression_status"] = "fail"
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
                    json.dumps(
                        {
                            "scenario": SCENARIO,
                            "topo_size": TOPO_SIZE,
                            "results": results,
                            "summary": {
                                "total": len(results),
                                "pass": sum(
                                    1
                                    for r in results
                                    if r["regression_status"] == "pass"
                                ),
                                "fail": sum(
                                    1
                                    for r in results
                                    if r["regression_status"] == "fail"
                                ),
                            },
                        },
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )

        failed = [r for r in results if r["regression_status"] != "pass"]
        assert not failed, json.dumps(failed, indent=2, default=str)
