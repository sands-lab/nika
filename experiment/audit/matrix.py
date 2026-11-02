"""Run one full Docker audit for every release case.

Labs of every resource class run concurrently, admitted one at a time while
free memory stays above a per-class floor and load stays under a ceiling.
Each class has a fixed number of concurrent slots, and the queue interleaves
classes with heavy labs first in each round. Each audit closes only the session
it started.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from itertools import zip_longest
import multiprocessing
import os
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import IO, Any

from experiment.audit import admission
from experiment.audit.environment import (
    CaseAudit,
    StageResult,
    identity_from_row,
)
from experiment.audit.live import audit_case, declared_probe
from experiment.audit.provenance import provenance_complete
from experiment.audit.report_doc import RESULTS_DIR
from nika.net_env.net_env_pool import (
    scenario_fixed_topo_size,
)
from nika.utils.session_artifacts import write_json_atomic
from nika.workflows.benchmark.admit import resource_class_for_row
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.workflows.benchmark.release import read_git_commit

_ROW_KEYS = (
    "scenario",
    "problem",
    "topo_size",
    "inject",
    "topo",
    "igp",
    "bgp_mode",
    "rpki",
    "backend",
    "device_profile",
    "traffic",
)


def _release_rows() -> list[dict[str, Any]]:
    from experiment.audit.coverage import release_cases

    return release_cases()


def effective_class(row: dict[str, Any]) -> str:
    """Resource class, including a scenario's baked topo size."""
    enriched = dict(row)
    if not str(enriched.get("topo_size") or ""):
        fixed = scenario_fixed_topo_size(str(enriched.get("scenario") or ""))
        if fixed:
            enriched["topo_size"] = fixed
    return resource_class_for_row(enriched)


def _copy_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = {key: row[key] for key in _ROW_KEYS if key in row and row[key] is not None}
    copied["scenario"] = str(row["scenario"])
    copied["problem"] = str(row.get("problem") or row.get("fault") or "")
    if "inject" in copied and isinstance(copied["inject"], dict):
        copied["inject"] = dict(copied["inject"])
    return copied


def audit_plan() -> list[dict[str, Any]]:
    """Return each concrete case from the frozen release exactly once."""
    rows = [_copy_row(row) for row in _release_rows()]
    seen: set[tuple[str, ...]] = set()
    plan: list[dict[str, Any]] = []
    for row in rows:
        key = identity_from_row(row).key()
        if key not in seen:
            seen.add(key)
            plan.append(row)
    return plan


def result_path(row: dict[str, Any], repeat: int = 0) -> Path:
    identity = identity_from_row(row)
    digest = hashlib.sha256(identity.key().encode()).hexdigest()[:10]
    return (
        RESULTS_DIR / f"{identity.scenario}__{identity.fault}__{digest}__r{repeat}.json"
    )


def agent_view_stages(row: dict[str, Any]) -> set[str]:
    """Agent-view stages this case records."""
    if is_healthy_case(str(row["problem"])):
        return {"agent_view"}
    return {"baseline_agent_view", "leak_scan"}


def result_current(
    row: dict[str, Any],
    repeat: int = 0,
    *,
    allow_dirty: bool = False,
) -> bool:
    """True when a stored run audited this case with its declared probe.

    Records from a dirty tree count only with ``allow_dirty``. A record also
    needs the agent-view stages.
    """
    path = result_path(row, repeat)
    if not path.is_file():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    audit = CaseAudit.model_validate(record["audit"])
    provenance = audit.provenance
    probe = (
        ""
        if is_healthy_case(str(row["problem"]))
        else declared_probe(str(row["problem"]))
    )
    if agent_view_stages(row) - {s.stage for s in audit.stages}:
        return False
    return (
        audit.identity.key() == identity_from_row(row).key()
        and audit.symptom_probe == probe
        and provenance_complete(provenance)
        and provenance is not None
        and (allow_dirty or not provenance.git_dirty)
    )


_MEMORY_FLOOR_GIB = admission.MEMORY_FLOOR_GIB
_ADMISSION_SPACING_SEC = admission.SPACING_SEC
# Labs keep growing after admission, so heavy classes also hold a slot for the whole case.
_CLASS_SLOTS = {"light": 8, "large": 3, "clab": 1, "k8s": 2}
_LOAD_CEILING = 2 * (os.cpu_count() or 1)
# Host-wide, so audits from several worktrees share the class slots.
_ADMISSION_LOCK = (
    Path(os.environ.get("NIKA_AUDIT_LOCK_DIR") or "/tmp/nika-audit-locks")
    / "admission.lock"
)


def _hold_class_slot(resource_class: str) -> IO[str]:
    return admission.hold_class_slot(
        resource_class, slots=_CLASS_SLOTS, lock_path=_ADMISSION_LOCK
    )


def wait_for_slot(row: dict[str, Any]) -> None:
    """Keep a per-class memory floor and a load ceiling while other NIKA workloads run."""
    admission.wait_for_admission(
        effective_class(row),
        lock_path=_ADMISSION_LOCK,
        ceiling=_LOAD_CEILING,
        on_wait=lambda info: print(
            f"waiting memory={info['memory_gib']}GiB floor={info['floor_gib']}GiB "
            f"load={info['load']:.0f} ceiling={_LOAD_CEILING} scenario={row['scenario']}",
            flush=True,
        ),
    )


def _evidence_text(stage: StageResult | None) -> str:
    if stage is None:
        return ""
    # Shrunk evidence stores a JSON snippet inside a string, so unescape once.
    return json.dumps(stage.evidence, default=str).replace('\\"', '"')


def diagnose(audit: CaseAudit, error: str | None = None) -> str:
    """Say whether a bad result comes from the fault or from the check."""
    if error:
        text = error.lower()
        if "not found locally" in text or "build it with" in text:
            return "verify: the scenario image is not installed on this host"
        if "cpu-sensitive http server failed" in text or "exceeded 20s" in text:
            return "verify: the HTTP readiness command timed out before a status code returned"
        if "verify_lab" in text or "evaluate_scenario" in text:
            return "verify: the lab health check raised before inject"
        return f"runner: {error.splitlines()[-1][:240]}"
    by_stage = {stage.stage: stage for stage in audit.stages}
    baseline = by_stage.get("baseline_lab")
    if baseline is not None and baseline.status == "fail":
        blob = _evidence_text(baseline)
        if '"rpki_rtr_connected": false' in blob:
            fault_ok = all(
                by_stage[name].status == "pass"
                for name in ("inject_artifact", "symptom", "final_symptom")
                if name in by_stage
            )
            if fault_ok and "inject_artifact" in by_stage:
                return (
                    "verify: verify_lab failed on rpki_rtr_connected; "
                    "the fault artifact and symptom passed"
                )
            return (
                "verify: verify_lab failed on rpki_rtr_connected; "
                "the other lab checks passed"
            )
        return "verify: verify_lab failed before inject"
    path = by_stage.get("baseline_path")
    if path is not None and path.status == "fail":
        evidence = path.evidence or {}
        probe = audit.symptom_probe or "the path probe"
        if evidence.get("control_plane_ok") is False:
            return f"verify: {probe} was already down before inject"
        if evidence.get("ping_ok") is False:
            return f"verify: {probe} was already down before inject"
        if evidence.get("http_ok") is False:
            return f"verify: {probe} was already down before inject"
        return "verify: the healthy path probe failed before inject"
    injected = by_stage.get("inject_artifact")
    if injected is not None and injected.status == "fail":
        return "case: verify_fault did not accept the injected artifact"
    for name in ("persistence_artifact", "final_artifact"):
        stage = by_stage.get(name)
        if stage is not None and stage.status == "fail":
            return "case: the artifact was absent before cleanup"
    symptom_stages = [
        by_stage[name]
        for name in ("symptom", "persistence_symptom", "final_symptom")
        if name in by_stage
    ]
    symptom_failed = [stage for stage in symptom_stages if stage.status == "fail"]
    if symptom_failed:
        blob = " ".join(_evidence_text(stage) for stage in symptom_failed)
        passed = any(stage.status == "pass" for stage in symptom_stages)
        if '"drops_delta": 0' in blob:
            return "case: the incast probe saw no queue-drop increase on the recorded egress"
        if '"nginx_saturated": false' in blob:
            return "case: nginx CPU stayed under the saturation ratio after inject"
        if "throughput_ratio" in blob:
            return "case: the transfer comparison did not show the contention effect"
        if (
            audit.identity.fault
            in {
                "link_packet_corruption",
                "device_forwarding_packet_corruption",
            }
            and '"ping_loss_percent": 0' in blob
            and passed
        ):
            return (
                "case: the corruption artifact stayed attached; "
                "some samples saw no ping loss"
            )
        if (
            audit.identity.fault
            in {
                "link_packet_corruption",
                "device_forwarding_packet_corruption",
            }
            and '"ping_loss_percent": 0' in blob
        ):
            return "case: the corruption artifact stayed attached and the samples saw no ping loss"
        return "case: the symptom probe did not observe the network effect"
    view = _agent_view_diagnosis(by_stage)
    if view:
        return view
    if audit.admission() == "pass":
        return "pass"
    if audit.admission() == "unsupported":
        weak = [stage for stage in audit.stages if stage.status != "pass"]
        if weak and all(
            stage.stage == "control_path" and stage.reason == "no_control_path"
            for stage in weak
        ):
            return "verify: the behavioral stages passed and this fault has no separate control path"
        return "verify: a recorded stage has no behavioral check"
    return audit.admission()


def _agent_view_diagnosis(by_stage: dict[str, StageResult]) -> str | None:
    """Name the first findings of a failed agent-view stage."""
    leak = by_stage.get("leak_scan")
    if leak is not None and leak.status == "fail":
        items = [
            f"{item.get('node')}:{item.get('path') or item.get('section')}"
            f" ({','.join(item.get('why') or [])})"
            for item in leak.evidence.get("flagged") or []
        ]
        return (
            f"leak: inject left {len(items)} fault-revealing artifact(s): "
            + "; ".join(items[:3])
        )[:300]
    for name in ("agent_view", "baseline_agent_view"):
        stage = by_stage.get(name)
        if stage is not None and stage.status == "fail":
            violated = stage.evidence.get("violated") or []
            count = stage.evidence.get("violated_count") or len(violated)
            return (
                f"agent-view: the healthy lab shows {count} fault-like reading(s): "
                + "; ".join(violated[:3])
            )[:300]
    return None


def _failed_audit(row: dict[str, Any], message: str) -> CaseAudit:
    fault = str(row.get("problem") or "")
    probe = "" if is_healthy_case(fault) else declared_probe(fault)
    status = "fail"
    if "not found locally" in message or "Build it with" in message:
        status = "unsupported"
    return CaseAudit(
        identity=identity_from_row(row),
        stages=[
            StageResult(stage="audit", status=status, reason=message[:500]),
        ],
        symptom_probe=probe,
    )


def run_audit_row(row: dict[str, Any]) -> dict[str, Any]:
    """Deploy, audit, and undeploy one case. The caller stores the record."""
    with _hold_class_slot(effective_class(row)):
        wait_for_slot(row)
        started = time.monotonic()
        error: str | None = None
        try:
            audit = audit_case(row)
        except Exception as exc:  # noqa: BLE001 - one case must not stop the matrix
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
            audit = _failed_audit(row, f"{type(exc).__name__}: {exc}")
    return {
        "audit": audit.model_dump(mode="json"),
        "diagnosis": diagnose(audit, None if error is None else error),
        "elapsed_sec": round(time.monotonic() - started, 1),
        # Keep the traceback tail: the first line alone hides where it failed.
        "error": None if error is None else error[-4000:],
        "resource_class": effective_class(row),
    }


def _store(row: dict[str, Any], repeat: int, record: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = result_path(row, repeat)
    write_json_atomic(path, record)
    audit = CaseAudit.model_validate(record["audit"])
    print(
        f"{audit.identity.scenario} {audit.identity.fault} r{repeat} "
        f"{audit.admission()} {record['diagnosis']}",
        flush=True,
    )


def run_matrix(
    *,
    jobs: int = 2,
    retry_failed: bool = False,
    scenario: str | None = None,
    fault: str | None = None,
    resource_class: str | None = None,
    force: bool = False,
    repeats: int = 1,
    allow_dirty: bool = False,
) -> bool:
    """Audit missing cases, or also rerun recorded non-passing cases.

    Each case runs ``repeats`` times on a fresh lab. A case passes only when
    every repeat passes.
    """
    if not allow_dirty and read_git_commit()[1]:
        raise SystemExit(
            "the working tree has uncommitted changes; commit them or pass --allow-dirty"
        )
    pending: list[tuple[dict[str, Any], int]] = []
    selected: list[dict[str, Any]] = []
    for row in audit_plan():
        if scenario and row["scenario"] != scenario:
            continue
        if fault and row["problem"] != fault:
            continue
        if resource_class and effective_class(row) != resource_class:
            continue
        selected.append(row)
        if force:
            # Repeats beyond this run's count would still feed the report.
            for stale in result_path(row, 0).parent.glob(
                result_path(row, 0).name.replace("__r0.json", "__r*.json")
            ):
                if int(stale.stem.rsplit("__r", 1)[1]) >= repeats:
                    stale.unlink()
        for repeat in range(repeats):
            path = result_path(row, repeat)
            if force or not result_current(row, repeat, allow_dirty=allow_dirty):
                pending.append((row, repeat))
            elif retry_failed:
                record = json.loads(path.read_text(encoding="utf-8"))
                if CaseAudit.model_validate(record["audit"]).admission() != "pass":
                    pending.append((row, repeat))
    by_class: dict[str, list[tuple[dict[str, Any], int]]] = {}
    for item in sorted(pending, key=lambda item: item[1]):
        by_class.setdefault(effective_class(item[0]), []).append(item)
    # Interleave classes so workers waiting for a heavy slot do not idle the pool.
    groups = [by_class.get(name, []) for name in ("k8s", "clab", "large", "light")]
    queue = [
        item for batch in zip_longest(*groups) for item in batch if item is not None
    ]
    _run_group(queue, jobs=max(1, jobs))
    passed = 0
    for row in selected:
        passed += all(
            result_current(row, repeat, allow_dirty=allow_dirty)
            and CaseAudit.model_validate(
                json.loads(result_path(row, repeat).read_text(encoding="utf-8"))[
                    "audit"
                ]
            ).admission()
            == "pass"
            for repeat in range(repeats)
        )
    print(f"Current passing audits: {passed}/{len(selected)}", flush=True)
    return bool(selected) and passed == len(selected)


def _run_group(items: list[tuple[dict[str, Any], int]], *, jobs: int) -> None:
    if not items:
        return
    workers = 1 if jobs < 2 or len(items) == 1 else jobs
    if workers == 1:
        for row, repeat in items:
            _store(row, repeat, run_audit_row(row))
        return
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        futures = {
            pool.submit(run_audit_row, row): (row, repeat) for row, repeat in items
        }
        for future in as_completed(futures):
            row, repeat = futures[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 - persist the worker failure
                record = {
                    "audit": _failed_audit(
                        row, f"{type(exc).__name__}: {exc}"
                    ).model_dump(mode="json"),
                    "diagnosis": f"runner: {exc}",
                    "elapsed_sec": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "resource_class": effective_class(row),
                }
            _store(row, repeat, record)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Audit every benchmark release case")
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--force", action="store_true", help="Rerun every selected case"
    )
    parser.add_argument("--scenario")
    parser.add_argument("--fault")
    parser.add_argument("--resource-class", choices=("light", "large", "k8s", "clab"))
    parser.add_argument(
        "--repeats", type=int, default=1, help="Fresh-lab runs per case"
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Run on uncommitted edits; such records never count for a release report",
    )
    args = parser.parse_args()
    ok = run_matrix(
        jobs=args.jobs,
        retry_failed=args.retry_failed,
        scenario=args.scenario,
        fault=args.fault,
        resource_class=args.resource_class,
        force=args.force,
        repeats=args.repeats,
        allow_dirty=args.allow_dirty,
    )
    raise SystemExit(0 if ok else 1)
