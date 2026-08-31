"""Parametrized scenario failure compatibility sweeps (inject + verify)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from nika.workflows.benchmark.inject_resolve import resolve_inject_params
from nika.net_env.p4_dc_fabric.fabric_manager.apply import reconcile_fabric
from nika.net_env.p4_dc_fabric.topology_model import build_clos_fabric_model
from nika.net_env.p4_dc_fabric.verify import verify_p4_dc_fabric_lab
from nika.net_env.p4_dc_gateway.apply import reconcile_gateway
from nika.net_env.p4_dc_gateway.topology_model import build_gateway_fabric_model
from nika.net_env.sdn_l3_clos.topology_model import build_clos_fabric_model as build_sdn_model
from nika.net_env.sdn_l3_clos.verify import verify_sdn_l3_clos_lab
from nika.net_env.verify import http_ok, ping_ok
from nika.problems.registry import (
    compatible,
    get_problem_instance,
    list_avail_problem_names,
)
from nika.runtime.factory import runtime_for_session
from nika.workflows.failure.inject import inject_failure as inject_failure_workflow
from tests.support.integration_base import IntegrationTestCase
from tests.support.prerequisites import docker_available
from tests.support.scenario_failure_compat import write_probe_report
from tests.support.symptom import evaluate_symptom, get_symptom_contract

TOPO_SIZE = "s"


def _compat_row(failure: str) -> dict[str, Any]:
    return {
        "failure": failure,
        "injection_success": False,
        "verify_fault": False,
        "regression_status": "fail",
        "error": None,
    }


def _append_compat_report(sweep: CompatSweep, results: list[dict[str, Any]]) -> None:
    write_probe_report(
        sweep.report_path,
        {"scenario": sweep.scenario, "results": results},
    )


def _resolve_compat_params(problem: str, scenario: str) -> dict[str, str]:
    return resolve_inject_params(problem, scenario, topo_size=TOPO_SIZE, seed=0)


def _verify_injected(
    problem: str,
    scenario: str,
    params: dict[str, str],
    scenario_kwargs: dict[str, Any],
) -> bool:
    problem_obj = get_problem_instance(
        [problem],
        scenario_name=scenario,
        **scenario_kwargs,
    )
    verify = problem_obj.verify_fault(params=problem_obj.Params(**params))
    return bool(verify.get("verified"))


@dataclass(frozen=True)
class CompatSweep:
    scenario: str
    failures: tuple[str, ...]
    report_path: Path
    mode: str  # sampled | p4runtime_loop | sdn_all | gateway_p4runtime


P4_FABRIC_SAMPLED = CompatSweep(
    scenario="p4_dc_fabric",
    failures=(
        "bmv2_switch_down",
        "link_down",
        "link_flap",
        "link_packet_corruption",
        "link_capacity_bottleneck",
        "host_incorrect_ip",
        "host_missing_ip",
        "p4_table_entry_missing",
        "p4_table_entry_misconfig",
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4runtime_pipeline_mismatch",
        "p4runtime_partial_write",
        "p4_table_resource_exhaustion",
    ),
    report_path=Path("results/test/p4_dc_fabric_failure_compat.json"),
    mode="sampled",
)

P4_FABRIC_P4RUNTIME = CompatSweep(
    scenario="p4_dc_fabric",
    failures=(
        "p4runtime_pipeline_mismatch",
        "p4runtime_partial_write",
        "p4_table_entry_missing",
        "p4_table_entry_misconfig",
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4_table_resource_exhaustion",
    ),
    report_path=Path("results/test/p4_dc_fabric_p4runtime_failures.json"),
    mode="p4runtime_loop",
)

SDN_L3_CLOS = CompatSweep(
    scenario="sdn_l3_clos",
    failures=tuple(),  # filled at runtime from registry
    report_path=Path("results/test/sdn_l3_clos_failure_compat.json"),
    mode="sdn_all",
)

P4_GATEWAY_P4RUNTIME = CompatSweep(
    scenario="p4_dc_gateway",
    failures=(
        "p4_table_entry_missing",
        "p4_table_entry_misconfig",
        "p4_action_selector_member_misconfig",
        "p4_ecmp_group_member_missing",
        "p4runtime_pipeline_mismatch",
        "p4runtime_partial_write",
        "p4_table_resource_exhaustion",
    ),
    report_path=Path("results/test/p4_dc_gateway_p4runtime_failures.json"),
    mode="gateway_p4runtime",
)

COMPAT_SWEEPS = (
    P4_FABRIC_SAMPLED,
    P4_FABRIC_P4RUNTIME,
    SDN_L3_CLOS,
    P4_GATEWAY_P4RUNTIME,
)


def _p4_fabric_smoke(runtime, model) -> dict[str, bool]:
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


def _sdn_smoke(runtime, model) -> dict[str, bool]:
    src = model.client_endpoints()[0]
    dst = next(w for w in model.web_endpoints() if w.leaf_id != src.leaf_id)
    return {
        "cross_rack_ping": ping_ok(runtime, src.name, dst.ip),
        "cross_rack_http": http_ok(runtime, src.name, f"http://{dst.ip}/"),
    }


def _sdn_observable(
    problem: str,
    *,
    verify_ok: bool,
    smoke_before: dict[str, bool],
    smoke_after: dict[str, bool],
    runtime,
    params: dict[str, Any],
) -> bool:
    contract = get_symptom_contract(problem)
    if contract.control_plane_only and verify_ok:
        return True
    if verify_ok and (
        smoke_before.get("cross_rack_ping") and not smoke_after.get("cross_rack_ping")
    ):
        return True
    if verify_ok and (
        smoke_before.get("cross_rack_http") and not smoke_after.get("cross_rack_http")
    ):
        return True
    if verify_ok:
        from pydantic import create_model

        param_model = create_model(
            "CompatParams", **{k: (str, v) for k, v in params.items()}
        )()
        ok, _ = evaluate_symptom(
            runtime,
            problem,
            param_model,
            scenario="sdn_l3_clos",
            topo_size=TOPO_SIZE,
            before=None,
        )
        if ok:
            return True
    return False


def _p4_fabric_observable(
    problem: str,
    *,
    verify_ok: bool,
    smoke_before: dict[str, bool],
    smoke_after: dict[str, bool],
    runtime,
    params: dict[str, Any],
) -> bool:
    if verify_ok and (
        smoke_before.get("cross_rack_ping") and not smoke_after.get("cross_rack_ping")
    ):
        return True
    if verify_ok and (
        smoke_before.get("cross_rack_http") and not smoke_after.get("cross_rack_http")
    ):
        return True
    if verify_ok:
        from pydantic import create_model

        param_model = create_model(
            "CompatParams", **{k: (str, v) for k, v in params.items()}
        )()
        ok, _ = evaluate_symptom(
            runtime,
            problem,
            param_model,
            scenario="p4_dc_fabric",
            topo_size=TOPO_SIZE,
            before=None,
        )
        if ok:
            return True
    return False


def _failures_for_sweep(sweep: CompatSweep) -> tuple[str, ...]:
    if sweep.mode == "sdn_all":
        return tuple(
            sorted(
                name
                for name in list_avail_problem_names()
                if compatible(name, sweep.scenario)
            )
        )
    return sweep.failures


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
@pytest.mark.parametrize("sweep", COMPAT_SWEEPS, ids=lambda s: s.mode)
class TestScenarioFailureCompat(IntegrationTestCase):
    def test_inject_and_verify(self, sweep: CompatSweep) -> None:
        failures = _failures_for_sweep(sweep)
        assert failures, f"expected failures for {sweep.scenario}"

        if sweep.mode == "gateway_p4runtime":
            self._run_gateway_p4runtime(sweep, failures)
            return
        if sweep.mode == "p4runtime_loop":
            self._run_p4_fabric_p4runtime_loop(sweep, failures)
            return

        results: list[dict[str, Any]] = []
        for problem in failures:
            row = _compat_row(problem)
            session_id: str | None = None
            try:
                params = _resolve_compat_params(problem, sweep.scenario)
                row["params"] = params
                session_id = self._start_env(sweep.scenario, ["-s", TOPO_SIZE])
                meta = self._assert_session_ready(session_id, sweep.scenario)
                runtime = runtime_for_session(meta)
                scenario_kwargs = self._scenario_kwargs(session_id)

                if sweep.scenario == "p4_dc_fabric":
                    model = build_clos_fabric_model(TOPO_SIZE)
                    healthy = verify_p4_dc_fabric_lab(
                        runtime, scenario_name=sweep.scenario, model=model
                    )
                    row["healthy_before"] = bool(healthy.get("verified"))
                    smoke_before = _p4_fabric_smoke(runtime, model)
                else:
                    model = build_sdn_model(TOPO_SIZE)
                    healthy = verify_sdn_l3_clos_lab(
                        runtime, scenario_name=sweep.scenario, model=model
                    )
                    row["healthy_before"] = bool(healthy.get("verified"))
                    smoke_before = _sdn_smoke(runtime, model)
                    if not row["healthy_before"]:
                        row["error"] = f"healthy verify failed: {healthy.get('checks')}"
                        results.append(row)
                        continue

                inject_failure_workflow(
                    [problem], session_id=session_id, param_overrides=params
                )
                row["injection_success"] = True
                time.sleep(2)

                verify_ok = _verify_injected(
                    problem,
                    sweep.scenario,
                    params,
                    scenario_kwargs,
                )
                row["verify_fault"] = verify_ok

                if sweep.scenario == "p4_dc_fabric":
                    row["smoke_before"] = smoke_before
                    row["smoke_after"] = _p4_fabric_smoke(runtime, model)
                    row["failure_observable"] = _p4_fabric_observable(
                        problem,
                        verify_ok=verify_ok,
                        smoke_before=smoke_before,
                        smoke_after=row["smoke_after"],
                        runtime=runtime,
                        params=params,
                    )
                    row["regression_status"] = (
                        "pass"
                        if row["injection_success"]
                        and verify_ok
                        and row["failure_observable"]
                        else "fail"
                    )
                else:
                    smoke_after = _sdn_smoke(runtime, model)
                    row["smoke_before"] = smoke_before
                    row["smoke_after"] = smoke_after
                    row["failure_observable"] = _sdn_observable(
                        problem,
                        verify_ok=verify_ok,
                        smoke_before=smoke_before,
                        smoke_after=smoke_after,
                        runtime=runtime,
                        params=params,
                    )
                    row["regression_status"] = (
                        "pass"
                        if row["injection_success"]
                        and verify_ok
                        and row["failure_observable"]
                        else "fail"
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
                _append_compat_report(sweep, results)

        failed = [r for r in results if r["regression_status"] != "pass"]
        assert not failed, failed

    def _run_p4_fabric_p4runtime_loop(
        self, sweep: CompatSweep, failures: tuple[str, ...]
    ) -> None:
        session_id = self._start_env(sweep.scenario, ["-s", TOPO_SIZE])
        results: list[dict[str, Any]] = []
        try:
            meta = self._assert_session_ready(session_id, sweep.scenario)
            runtime = runtime_for_session(meta)
            model = build_clos_fabric_model(TOPO_SIZE)
            healthy = verify_p4_dc_fabric_lab(
                runtime, scenario_name=sweep.scenario, model=model
            )
            assert healthy.get("verified"), healthy

            for problem in failures:
                row = _compat_row(problem)
                try:
                    params = _resolve_compat_params(problem, sweep.scenario)
                    params["host_name"] = "leaf_1"
                    row["params"] = params
                    inject_failure_workflow(
                        [problem], session_id=session_id, param_overrides=params
                    )
                    row["injection_success"] = True
                    row["verify_fault"] = _verify_injected(
                        problem,
                        sweep.scenario,
                        params,
                        self._scenario_kwargs(session_id),
                    )
                    row["smoke_after"] = _p4_fabric_smoke(runtime, model)
                    reconcile_fabric(runtime, model)
                    restored = _p4_fabric_smoke(runtime, model)
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
                _append_compat_report(sweep, results)
        finally:
            self._close_session(session_id)

        failed = [r for r in results if r["regression_status"] != "pass"]
        assert not failed, failed

    def _run_gateway_p4runtime(
        self, sweep: CompatSweep, failures: tuple[str, ...]
    ) -> None:
        session_id = self._start_env(sweep.scenario, ["-s", TOPO_SIZE])
        try:
            meta = self._assert_session_ready(session_id, sweep.scenario)
            runtime = runtime_for_session(meta)
            model = build_gateway_fabric_model(TOPO_SIZE)
            source = model.clients[0]
            url = model.web_urls[0]
            assert http_ok(runtime, source.name, url)

            for problem in failures:
                params = _resolve_compat_params(problem, sweep.scenario)
                if problem == "p4runtime_pipeline_mismatch":
                    params["host_name"] = "gateway_1"
                inject_failure_workflow(
                    [problem], session_id=session_id, param_overrides=params
                )
                instance = get_problem_instance(
                    [problem],
                    scenario_name=sweep.scenario,
                    **self._scenario_kwargs(session_id),
                )
                verified = instance.verify_fault(params=instance.Params(**params))
                assert verified.get("verified"), (problem, verified)
                reconcile_gateway(runtime, model)
                assert http_ok(runtime, source.name, url), problem
        finally:
            self._close_session(session_id)


@pytest.mark.integration
@pytest.mark.skipif(not docker_available(), reason="Docker not available")
class TestP4RuntimePipelineMismatchStability(IntegrationTestCase):
    """Three consecutive inject→verify→symptom→reconcile cycles in one session."""

    def test_p4_dc_fabric_stable_reproduction(self) -> None:
        problem = "p4runtime_pipeline_mismatch"
        session_id = self._start_env("p4_dc_fabric", ["-s", TOPO_SIZE])
        try:
            meta = self._assert_session_ready(session_id, "p4_dc_fabric")
            runtime = runtime_for_session(meta)
            model = build_clos_fabric_model(TOPO_SIZE)
            scenario_kwargs = self._scenario_kwargs(session_id)
            healthy = verify_p4_dc_fabric_lab(
                runtime, scenario_name="p4_dc_fabric", model=model
            )
            assert healthy.get("verified"), healthy

            for cycle in range(3):
                params = _resolve_compat_params(problem, "p4_dc_fabric")
                assert params["host_name"] == "leaf_1", params
                smoke_before = _p4_fabric_smoke(runtime, model)
                assert smoke_before["cross_rack_ping"], smoke_before

                inject_failure_workflow(
                    [problem], session_id=session_id, param_overrides=params
                )
                time.sleep(2)

                verify_ok = _verify_injected(
                    problem, "p4_dc_fabric", params, scenario_kwargs
                )
                assert verify_ok, f"cycle {cycle}: verify failed"

                from pydantic import create_model

                param_model = create_model(
                    "CompatParams", **{k: (str, v) for k, v in params.items()}
                )()
                symptom_ok, symptom = evaluate_symptom(
                    runtime,
                    problem,
                    param_model,
                    scenario="p4_dc_fabric",
                    topo_size=TOPO_SIZE,
                    before=None,
                )
                assert symptom_ok, f"cycle {cycle}: {symptom}"

                smoke_after = _p4_fabric_smoke(runtime, model)
                assert not smoke_after["cross_rack_ping"], (
                    f"cycle {cycle}: ping still ok"
                )

                reconcile_fabric(runtime, model)
                restored = _p4_fabric_smoke(runtime, model)
                assert restored["cross_rack_ping"], f"cycle {cycle}: restore failed"
        finally:
            self._close_session(session_id)
