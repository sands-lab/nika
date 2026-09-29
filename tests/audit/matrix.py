"""Run one full Docker audit for every scenario and every failure.

Light labs may run while other light labs are up. Exclusive labs
(Containerlab, Kubernetes, XRd, topo size ``l``) wait until this host has
no containers. Each audit closes only the session it started.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from nika.audit.environment import CaseAudit, StageResult, identity_from_row
from nika.net_env.net_env_pool import (
    list_all_net_envs,
    parse_column,
    scenario_fixed_topo_size,
    scenario_requires_topo_size,
)
from nika.problems.registry import compatible_columns, list_avail_problem_names
from nika.workflows.benchmark.admit import resource_class_for_row
from nika.workflows.benchmark.healthy import is_healthy_case

from tests.audit.live import audit_case, declared_probe
from tests.audit.report_doc import RESULTS_DIR

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
    from nika.audit.coverage import release_cases

    return release_cases()


def effective_class(row: dict[str, Any]) -> str:
    """Resource class, including a scenario's baked topo size."""
    enriched = dict(row)
    if not str(enriched.get("topo_size") or ""):
        fixed = scenario_fixed_topo_size(str(enriched.get("scenario") or ""))
        if fixed:
            enriched["topo_size"] = fixed
    return resource_class_for_row(enriched)


def _rank(row: dict[str, Any]) -> tuple:
    cls = effective_class(row)
    exclusive = 0 if cls == "light" else 1
    backend = 0 if str(row.get("backend") or "kathara") == "kathara" else 1
    size = str(row.get("topo_size") or "")
    size_rank = {"s": 0, "": 1, "m": 2, "l": 3}.get(size, 4)
    return (exclusive, backend, size_rank, str(row.get("scenario") or ""))


def _copy_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = {key: row[key] for key in _ROW_KEYS if key in row and row[key] is not None}
    copied["scenario"] = str(row["scenario"])
    copied["problem"] = str(row.get("problem") or row.get("fault") or "")
    if "inject" in copied and isinstance(copied["inject"], dict):
        copied["inject"] = dict(copied["inject"])
    return copied


def _light_column_row(problem: str) -> dict[str, Any] | None:
    from nika.workflows.benchmark.inject_resolve import resolve_inject_params

    for column in compatible_columns(problem):
        scenario, config = parse_column(column)
        row: dict[str, Any] = {"scenario": scenario, "problem": problem}
        if scenario_requires_topo_size(scenario):
            row["topo_size"] = "s"
        spec = list_all_net_envs().get(scenario)
        if spec is not None and "kathara" in spec.supported_backends:
            row["backend"] = "kathara"
        elif spec is not None and spec.supported_backends:
            row["backend"] = spec.supported_backends[0]
        if config == "isis":
            row["igp"] = "isis"
        elif config == "ospf":
            row["igp"] = "ospf"
        elif config == "ibgp_rr":
            row["bgp_mode"] = "ibgp_rr"
        elif config == "ebgp":
            row["bgp_mode"] = "ebgp"
        if effective_class(row) != "light":
            continue
        isp = {
            key: row[key]
            for key in ("igp", "bgp_mode", "rpki", "backend", "device_profile")
            if key in row
        }
        row["inject"] = resolve_inject_params(
            problem,
            scenario,
            str(row.get("topo_size") or ""),
            seed=1,
            isp_options=isp or None,
        )
        return row
    return None


def _healthy_row(scenario: str) -> dict[str, Any]:
    matches = [
        row
        for row in _release_rows()
        if row.get("scenario") == scenario and row.get("problem") == "healthy"
    ]
    spec = list_all_net_envs()[scenario]
    if matches:
        row = _copy_row(sorted(matches, key=_rank)[0])
    else:
        row = {"scenario": scenario, "problem": "healthy"}
        if scenario_requires_topo_size(scenario):
            row["topo_size"] = "s"
        if "kathara" in spec.supported_backends:
            row["backend"] = "kathara"
        elif spec.supported_backends:
            row["backend"] = spec.supported_backends[0]
    if row.get("backend") == "containerlab" and "kathara" in spec.supported_backends:
        row["backend"] = "kathara"
        row.pop("device_profile", None)
    return row


def _failure_row(problem: str) -> dict[str, Any]:
    matches = [row for row in _release_rows() if row.get("problem") == problem]
    light = [row for row in matches if effective_class(row) == "light"]
    if light:
        return _copy_row(sorted(light, key=_rank)[0])
    synthetic = _light_column_row(problem)
    if synthetic is not None:
        return synthetic
    if matches:
        return _copy_row(sorted(matches, key=_rank)[0])
    cols = compatible_columns(problem)
    scenario = parse_column(cols[0])[0] if cols else ""
    return {"scenario": scenario, "problem": problem}


def audit_plan() -> list[dict[str, Any]]:
    """One healthy row per scenario, then one row per failure."""
    scenarios = [_healthy_row(name) for name in sorted(list_all_net_envs())]
    failures = [_failure_row(name) for name in sorted(list_avail_problem_names())]
    return scenarios + failures


def result_path(row: dict[str, Any]) -> Path:
    identity = identity_from_row(row)
    raw = "|".join(identity.key())
    digest = hashlib.sha256(raw.encode()).hexdigest()[:10]
    return RESULTS_DIR / f"{identity.scenario}__{identity.fault}__{digest}.json"


def _container_count() -> int:
    output = subprocess.check_output(["docker", "ps", "-q"], text=True)
    return len([line for line in output.splitlines() if line.strip()])


def _available_gib() -> float:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    return 0.0


_EXTRA_LOCK = Path("/tmp/nika-env-audit-extra.lock")


def _lock_holder_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _try_extra_lock() -> bool:
    """Allow one light lab beside an existing benchmark lab."""
    try:
        fd = os.open(_EXTRA_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        if _lock_holder_alive(_EXTRA_LOCK):
            return False
        _EXTRA_LOCK.unlink(missing_ok=True)
        return _try_extra_lock()
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def _release_extra_lock() -> None:
    if not _EXTRA_LOCK.is_file():
        return
    try:
        holder = int(_EXTRA_LOCK.read_text(encoding="utf-8").strip() or "0")
    except (OSError, ValueError):
        return
    if holder == os.getpid():
        _EXTRA_LOCK.unlink(missing_ok=True)


def wait_for_slot(row: dict[str, Any]) -> None:
    """Wait until this host can take ``row`` without stacking an exclusive lab."""
    exclusive = effective_class(row) != "light"
    while True:
        count = _container_count()
        memory = _available_gib()
        if exclusive:
            if count == 0 and memory >= 8:
                return
        elif memory >= 8 and count < 40:
            return
        elif memory >= 8 and count < 130 and _try_extra_lock():
            return
        print(
            f"waiting containers={count} memory={memory:.1f}GiB exclusive={exclusive}",
            flush=True,
        )
        time.sleep(20)


def _shrink(audit: CaseAudit) -> dict[str, Any]:
    data = audit.model_dump(mode="json")
    for stage in data.get("stages") or []:
        evidence = stage.get("evidence") or {}
        blob = json.dumps(evidence, default=str)
        if len(blob) > 600:
            stage["evidence"] = {"summary": blob[:600]}
    return data


def diagnose(audit: CaseAudit, error: str | None = None) -> str:
    """Say whether a bad result comes from the fault or from the check."""
    if error:
        text = error.lower()
        if "verify_lab" in text or "evaluate_scenario" in text:
            return "verify: the lab health check raised before inject"
        return f"runner: {error.splitlines()[-1][:240]}"
    by_stage = {stage.stage: stage for stage in audit.stages}
    baseline = by_stage.get("baseline_lab")
    if baseline is not None and baseline.status == "fail":
        return "verify: verify_lab failed before inject"
    path = by_stage.get("baseline_path")
    if path is not None and path.status == "fail":
        return "verify: the healthy path probe failed before inject"
    injected = by_stage.get("inject_artifact")
    if injected is not None and injected.status == "fail":
        return "case: verify_fault did not accept the injected artifact"
    for name in ("persistence_artifact", "final_artifact"):
        stage = by_stage.get(name)
        if stage is not None and stage.status == "fail":
            return "case: the artifact was absent before cleanup"
    symptom_failed = False
    artifact_only = False
    for name in ("symptom", "persistence_symptom", "final_symptom"):
        stage = by_stage.get(name)
        if stage is None:
            continue
        if stage.status == "fail":
            symptom_failed = True
        if stage.status == "no_evidence" and stage.reason == "artifact_only":
            artifact_only = True
    if symptom_failed:
        return "case: the symptom probe did not observe the network effect"
    if artifact_only:
        return "verify: the symptom contract is artifact_only, so this run has no network-effect observation"
    if audit.admission() == "pass":
        return "pass"
    if audit.admission() == "unsupported":
        return "verify: a recorded stage has no behavioral check"
    return audit.admission()


def _failed_audit(row: dict[str, Any], message: str) -> CaseAudit:
    fault = str(row.get("problem") or "")
    probe = "" if is_healthy_case(fault) else declared_probe(fault)
    return CaseAudit(
        identity=identity_from_row(row),
        stages=[
            StageResult(stage="audit", status="fail", reason=message[:500]),
        ],
        symptom_probe=probe,
    )


def run_audit_row(row: dict[str, Any]) -> dict[str, Any]:
    """Deploy, audit, and undeploy one case. The caller stores the record."""
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
    finally:
        _release_extra_lock()
    return {
        "audit": _shrink(audit),
        "diagnosis": diagnose(audit, None if error is None else error),
        "elapsed_sec": round(time.monotonic() - started, 1),
        "error": None if error is None else error.splitlines()[0][:500],
        "resource_class": effective_class(row),
    }


def _store(row: dict[str, Any], record: dict[str, Any]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = result_path(row)
    path.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    audit = CaseAudit.model_validate(record["audit"])
    print(
        f"{audit.identity.scenario} {audit.identity.fault} "
        f"{audit.admission()} {record['diagnosis']}",
        flush=True,
    )


def run_matrix(*, jobs: int = 2) -> None:
    """Audit every planned row. Finished result files are left in place."""
    pending = [row for row in audit_plan() if not result_path(row).is_file()]
    light = [row for row in pending if effective_class(row) == "light"]
    heavy = [row for row in pending if effective_class(row) != "light"]
    _run_group(light, jobs=max(1, jobs))
    _run_group(heavy, jobs=1)


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
                    "audit": _shrink(
                        _failed_audit(row, f"{type(exc).__name__}: {exc}")
                    ),
                    "diagnosis": f"runner: {exc}",
                    "elapsed_sec": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                    "resource_class": effective_class(row),
                }
            _store(row, record)
