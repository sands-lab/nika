"""Run one full Docker audit for every release case.

Labs of every resource class run concurrently, admitted one at a time while
free memory stays above a per-class floor and load stays under a ceiling.
Each class has a fixed number of concurrent slots, and the queue interleaves
classes with heavy labs first in each round. Each audit closes only the session
it started.
"""

from __future__ import annotations

import argparse
import fcntl
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

from experiment.audit.environment import CaseAudit, StageResult, identity_from_row
from experiment.audit.provenance import provenance_complete
from nika.net_env.net_env_pool import (
    scenario_fixed_topo_size,
)
from nika.workflows.benchmark.admit import resource_class_for_row
from nika.workflows.benchmark.healthy import is_healthy_case
from nika.utils.session_artifacts import write_json_atomic

from experiment.audit.live import audit_case, declared_probe
from experiment.audit.report_doc import RESULTS_DIR

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


def result_path(row: dict[str, Any]) -> Path:
    identity = identity_from_row(row)
    raw = "|".join(identity.key())
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return RESULTS_DIR / f"{identity.scenario}__{identity.fault}__{digest}.json"


def result_current(row: dict[str, Any]) -> bool:
    """True when a stored run used the currently declared symptom probe."""
    path = result_path(row)
    if not path.is_file():
        return False
    record = json.loads(path.read_text(encoding="utf-8"))
    audit = CaseAudit.model_validate(record["audit"])
    probe = (
        ""
        if is_healthy_case(str(row["problem"]))
        else declared_probe(str(row["problem"]))
    )
    return (
        audit.identity.key() == identity_from_row(row).key()
        and audit.symptom_probe == probe
        and provenance_complete(audit.provenance)
    )


_MEMORY_FLOOR_GIB = {"light": 8, "large": 16, "clab": 32, "k8s": 16}
# SR Linux nodes keep claiming memory for minutes after a clab lab deploys.
_ADMISSION_SPACING_SEC = {"light": 10, "large": 45, "clab": 180, "k8s": 90}
# Labs keep growing after admission, so heavy classes also hold a slot for the whole case.
_CLASS_SLOTS = {"light": 8, "large": 3, "clab": 1, "k8s": 2}
_LOAD_CEILING = 2 * (os.cpu_count() or 1)
_ADMISSION_LOCK = RESULTS_DIR.parent / ".environment-audit-admission.lock"


def _available_gib() -> float:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


def _hold_class_slot(resource_class: str) -> IO[str]:
    """Block until one of the class's slots is free; the slot lasts until the file closes."""
    _ADMISSION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    while True:
        for index in range(_CLASS_SLOTS[resource_class]):
            handle = Path(f"{_ADMISSION_LOCK}.{resource_class}.{index}").open("w")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                continue
            return handle
        time.sleep(20)


def wait_for_slot(row: dict[str, Any]) -> None:
    """Keep a per-class memory floor and a load ceiling while other NIKA workloads run.

    Admissions are serialized and spaced so the next check sees the memory the
    previous lab claimed while deploying.
    """
    resource_class = effective_class(row)
    floor = _MEMORY_FLOOR_GIB[resource_class]
    _ADMISSION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    while True:
        # Release the lock between checks so a waiting heavy lab does not block
        # smaller cases that still fit.
        with _ADMISSION_LOCK.open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            memory = _available_gib()
            load = os.getloadavg()[0]
            if memory >= floor and load < _LOAD_CEILING:
                time.sleep(_ADMISSION_SPACING_SEC[resource_class])
                return
        print(
            f"waiting memory={memory:.1f}GiB floor={floor}GiB "
            f"load={load:.0f} ceiling={_LOAD_CEILING} scenario={row['scenario']}",
            flush=True,
        )
        time.sleep(20)


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
    artifact_only = any(
        stage.status == "no_evidence" and stage.reason == "artifact_only"
        for stage in symptom_stages
    )
    if symptom_failed:
        blob = " ".join(_evidence_text(stage) for stage in symptom_failed)
        passed = any(stage.status == "pass" for stage in symptom_stages)
        if audit.symptom_probe == "artifact_only":
            if by_stage.get("symptom") and by_stage["symptom"].status == "fail":
                return "verify: scenario health checks did not expose the fault effect"
            return "case: the observed scenario health regression did not persist"
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
    if artifact_only:
        return "verify: the symptom contract is artifact_only, so this run has no network-effect observation"
    if any(stage.reason == "control_plane_only" for stage in audit.stages):
        return "verify: the symptom contract is control_plane_only, so this run has no data-plane observation"
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
            if is_healthy_case(str(row.get("problem") or "")):
                audit = audit_case(row, window_sec=2)
            else:
                audit = audit_case(row)
        except Exception as exc:  # noqa: BLE001 - one case must not stop the matrix
            error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
            audit = _failed_audit(row, f"{type(exc).__name__}: {exc}")
    return {
        "audit": audit.model_dump(mode="json"),
        "diagnosis": diagnose(audit, None if error is None else error),
        "elapsed_sec": round(time.monotonic() - started, 1),
        "error": None if error is None else error.splitlines()[0][:500],
        "resource_class": effective_class(row),
    }


def _store(row: dict[str, Any], record: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = result_path(row)
    write_json_atomic(path, record)
    audit = CaseAudit.model_validate(record["audit"])
    print(
        f"{audit.identity.scenario} {audit.identity.fault} "
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
) -> bool:
    """Audit missing cases, or also rerun recorded non-passing cases."""
    pending: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    for row in audit_plan():
        if scenario and row["scenario"] != scenario:
            continue
        if fault and row["problem"] != fault:
            continue
        if resource_class and effective_class(row) != resource_class:
            continue
        selected.append(row)
        path = result_path(row)
        if force or not result_current(row):
            pending.append(row)
        elif retry_failed:
            record = json.loads(path.read_text(encoding="utf-8"))
            if CaseAudit.model_validate(record["audit"]).admission() != "pass":
                pending.append(row)
    by_class: dict[str, list[dict[str, Any]]] = {}
    for row in pending:
        by_class.setdefault(effective_class(row), []).append(row)
    # Interleave classes so workers waiting for a heavy slot do not idle the pool.
    groups = [by_class.get(name, []) for name in ("k8s", "clab", "large", "light")]
    queue = [row for batch in zip_longest(*groups) for row in batch if row is not None]
    _run_group(queue, jobs=max(1, jobs))
    passed = 0
    for row in selected:
        if result_current(row):
            record = json.loads(result_path(row).read_text(encoding="utf-8"))
            passed += CaseAudit.model_validate(record["audit"]).admission() == "pass"
    print(f"Current passing audits: {passed}/{len(selected)}", flush=True)
    return bool(selected) and passed == len(selected)


def _run_group(rows: list[dict[str, Any]], *, jobs: int) -> None:
    if not rows:
        return
    workers = 1 if jobs < 2 or len(rows) == 1 else jobs
    if workers == 1:
        for row in rows:
            _store(row, run_audit_row(row))
        return
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        futures = {pool.submit(run_audit_row, row): row for row in rows}
        for future in as_completed(futures):
            row = futures[future]
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
            _store(row, record)


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
    args = parser.parse_args()
    ok = run_matrix(
        jobs=args.jobs,
        retry_failed=args.retry_failed,
        scenario=args.scenario,
        fault=args.fault,
        resource_class=args.resource_class,
        force=args.force,
    )
    raise SystemExit(0 if ok else 1)
