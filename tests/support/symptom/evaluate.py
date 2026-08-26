"""Unified test-path ``evaluate_symptom`` for every registered failure."""

from __future__ import annotations

from typing import Any

from nika.net_env.verify import compare_symptom
from tests.support.symptom.types import ProbeSnapshot
from nika.runtime.base import LabRuntime
from tests.support.symptom.contracts import get_symptom_contract
from tests.support.symptom.custom import evaluate_custom_symptom
from tests.support.symptom.probe import (
    _resolve_blackhole_path,
    _resolve_path,
    run_probe_snapshot,
    symptom_class_to_expect,
)


def evaluate_symptom(
    runtime: LabRuntime,
    failure: str,
    params: Any,
    *,
    scenario: str | None,
    topo_size: str = "s",
    before: ProbeSnapshot | None = None,
    problem: Any | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Confirm expected network impact after inject (tests only).

    Uses the failure's symptom contract ``probe``. Failures with
    ``probe="custom"`` require a live ``problem`` instance and dispatch to
    ``tests.support.symptom.custom``.
    """
    contract = get_symptom_contract(failure)
    if contract.control_plane_only:
        return True, {
            "skipped": True,
            "reason": "control_plane_only",
            "symptom_class": contract.symptom_class,
        }
    if contract.probe == "artifact_only":
        return True, {
            "skipped": True,
            "reason": "artifact_only",
            "symptom_class": contract.symptom_class,
        }
    if contract.probe == "custom":
        if problem is None:
            return False, {
                "error": "custom_requires_problem_instance",
                "symptom_class": contract.symptom_class,
            }
        return evaluate_custom_symptom(failure, problem, params)

    path = _resolve_path(scenario, params, topo_size=topo_size)
    if path is None:
        return False, {"error": "no_probe_path", "scenario": scenario}
    if failure == "host_static_blackhole":
        path = _resolve_blackhole_path(runtime, params, path)
    after = run_probe_snapshot(runtime, contract.probe, path, params=params)
    before_snap = before if before is not None else ProbeSnapshot()
    expect = symptom_class_to_expect(contract.symptom_class)
    if contract.symptom_class == "gray":
        expect = "gray_loss"

    if contract.probe == "ping_old_ip":
        ok = after.ping_ok is False
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "symptom_class": contract.symptom_class,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "old_ip_unreachable",
                "observed": not after.ping_ok,
            },
        }
    if contract.probe == "route_get_onlink":
        ok = bool(after.extra.get("route_onlink"))
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "symptom_class": contract.symptom_class,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": "route_onlink",
                "observed": after.extra.get("route_onlink"),
            },
        }
    if contract.probe == "iperf_throughput":
        bps = after.extra.get("bits_per_second")
        before_bps = before_snap.extra.get("bits_per_second")
        if before_bps is None:
            before_bps = before_snap.as_dict().get("bits_per_second")
        overlimits = after.extra.get("tbf_overlimits")
        ok = False
        if bps is not None:
            ok = float(bps) < 100_000.0 or (
                before_bps is not None and float(bps) < float(before_bps) * 0.5
            )
        if (
            not ok
            and failure != "link_capacity_bottleneck"
            and overlimits is not None
            and int(overlimits) > 0
        ):
            ok = True
        expect_label = (
            "low_throughput"
            if failure == "link_capacity_bottleneck"
            else "low_throughput_or_tbf_overlimits"
        )
        return ok, {
            "failure": failure,
            "probe": contract.probe,
            "symptom_class": contract.symptom_class,
            "before": before_snap.as_dict(),
            "after": after.as_dict(),
            "comparison": {
                "expect": expect_label,
                "bps": bps,
                "before_bps": before_bps,
                "tbf_overlimits": overlimits,
            },
        }

    ok, cmp_details = compare_symptom(
        before_snap.as_dict(),
        after.as_dict(),
        expect,  # type: ignore[arg-type]
        loss_min_percent=contract.loss_min_percent,
        latency_factor=contract.latency_factor,
    )
    if not ok and contract.symptom_class in {"degradation", "latency"}:
        after_ms = after.http_time_ms
        before_ms = before_snap.http_time_ms
        if (
            before_ms is not None
            and before_ms < 500.0
            and after_ms is not None
            and after_ms >= 500.0
        ):
            ok = True
            cmp_details["absolute_latency_pass"] = after_ms
    return ok, {
        "failure": failure,
        "probe": contract.probe,
        "symptom_class": contract.symptom_class,
        "before": before_snap.as_dict(),
        "after": after.as_dict(),
        "comparison": cmp_details,
    }
