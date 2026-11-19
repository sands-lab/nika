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

from scripts.benchmark_audit import admission
from scripts.benchmark_audit.environment import (
    CaseAudit,
    StageResult,
    identity_from_row,
)
from scripts.benchmark_audit.live import audit_case, audit_case_image, declared_probe
from scripts.benchmark_audit.provenance import provenance_current
from nika.config import REPO_ROOT, RUNTIME_DIR
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
    "problems",
    "schedule",
    "telemetry",
    "topo_size",
    "inject",
    "topo",
    "igp",
    "bgp_mode",
    "rpki",
    "backend",
    "device_profile",
    "traffic",
    "objectives",
    "root_causes",
    "root_causes_status",
)


RESULTS_DIR = Path(
    os.environ.get("NIKA_AUDIT_RESULTS_DIR")
    or RUNTIME_DIR / "environment-audit-results"
)
# Audit the Firecracker case images trials restore instead of fresh labs. An
# environment variable, so spawned workers inherit the mode.
CASE_IMAGES_ENV = "NIKA_AUDIT_CASE_IMAGES"


def case_image_mode() -> bool:
    return os.environ.get(CASE_IMAGES_ENV) == "1"


def results_dir() -> Path:
    return RESULTS_DIR / "case-images" if case_image_mode() else RESULTS_DIR


# The release whose cases the matrix audits; labs deploy its environments.
AUDITED_RELEASE = "0.3.0"


def _release_rows() -> list[dict[str, Any]]:
    from nika.workflows.benchmark.release import load_release

    return [
        row
        for split in ("dev", "test")
        for row in load_release(AUDITED_RELEASE, split=split).cases
    ]


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
        results_dir()
        / f"{identity.scenario}__{identity.fault}__{digest}__r{repeat}.json"
    )


def agent_view_stages(row: dict[str, Any]) -> set[str]:
    """Agent-view stages this case records."""
    if is_healthy_case(str(row["problem"])):
        return {"agent_view"}
    return {"baseline_agent_view", "leak_scan"}


def result_current(
    row: dict[str, Any],
    repeat: int = 0,
) -> bool:
    """True when the current clean commit has complete evidence for this case."""
    path = result_path(row, repeat)
    if not path.is_file():
        return False
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        audit = CaseAudit.model_validate(record["audit"])
    except (ValueError, KeyError, TypeError):
        return False
    provenance = audit.provenance
    probe = (
        ""
        if is_healthy_case(str(row["problem"]))
        else declared_probe(str(row["problem"]))
    )
    expected = {"oracle"} if case_image_mode() else agent_view_stages(row) | {"oracle"}
    if expected - {s.stage for s in audit.stages}:
        return False
    return (
        audit.identity.key() == identity_from_row(row).key()
        and audit.symptom_probe == probe
        and provenance_current(provenance)
    )


_MEMORY_FLOOR_GIB = admission.MEMORY_FLOOR_GIB
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
    resource_class = effective_class(row)
    floors = dict(_MEMORY_FLOOR_GIB)
    if not case_image_mode() and row.get("device_profile") in {"iosxr", "routeros"}:
        from nika.net_env.net_env_pool import (
            get_net_env_instance,
            scenario_requires_topo_size,
        )

        kwargs = {
            key: row[key]
            for key in ("backend", "device_profile", "topo", "igp", "bgp_mode", "rpki")
            if row.get(key) is not None
        }
        if scenario_requires_topo_size(row["scenario"]):
            kwargs["topo_size"] = row.get("topo_size") or "s"
        env = get_net_env_instance(row["scenario"], **kwargs)
        from nika.telemetry import TELEMETRY_KINDS, apply_telemetry, normalize_telemetry

        kinds = normalize_telemetry(row.get("telemetry", TELEMETRY_KINDS))
        if kinds:
            apply_telemetry(env, kinds)
        required = env.native_lab_memory()
        if required is not None:
            floors[resource_class] = max(floors[resource_class], required / 1024**3)
    admission.wait_for_admission(
        resource_class,
        lock_path=_ADMISSION_LOCK,
        ceiling=_LOAD_CEILING,
        floors=floors,
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
        if "xrd control plane image" in text and "not found locally" in text:
            return (
                "verify: the licensed IOS XR image is missing; "
                "nika images prepare excludes licensed images"
            )
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
            from nika.net_env.utils.native_admission import NativeLabMemoryError

            run = audit_case_image if case_image_mode() else audit_case
            while True:
                try:
                    audit = run(row, release=AUDITED_RELEASE)
                    break
                except NativeLabMemoryError:
                    wait_for_slot(row)
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
    path = result_path(row, repeat)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(path, record)
    audit = CaseAudit.model_validate(record["audit"])
    print(
        f"{audit.identity.scenario} {audit.identity.fault} r{repeat} "
        f"{audit.admission()} {record['diagnosis']}",
        flush=True,
    )


def case_id(row: dict[str, Any]) -> str:
    """Stable case name shared by result files and ``--cases`` lists."""
    return result_path(row, 0).name.removesuffix("__r0.json")


def _shard_of(row: dict[str, Any], count: int) -> int:
    """Stable shard index of a case, independent of host and catalog order."""
    digest = hashlib.sha256(result_path(row, 0).name.encode()).hexdigest()
    return int(digest, 16) % count


def _parse_shard(value: str) -> tuple[int, int]:
    index, _, count = value.partition("/")
    shard = (int(index), int(count))
    if not 0 <= shard[0] < shard[1]:
        raise argparse.ArgumentTypeError("--shard expects K/N with 0 <= K < N")
    return shard


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
    shard: tuple[int, int] | None = None,
    cases: set[str] | None = None,
) -> bool:
    """Audit missing cases, or also rerun recorded non-passing cases.

    Each case runs ``repeats`` times on a fresh lab. A case passes only when
    every repeat passes.
    """
    if Path(__file__).resolve().parents[2] != REPO_ROOT.resolve():
        raise SystemExit(
            "the audit scripts and NIKA runtime come from different checkouts; "
            "run from the runtime checkout using its uv environment"
        )
    if not allow_dirty and read_git_commit()[1]:
        raise SystemExit(
            "the working tree has uncommitted changes; commit them or pass --allow-dirty"
        )
    if repeats < 1:
        raise ValueError("repeats must be at least 1")
    plan = audit_plan()
    if cases is not None:
        unknown = cases - {case_id(row) for row in plan}
        if unknown:
            raise ValueError(f"Unknown audit cases: {sorted(unknown)}")
    pending: list[tuple[dict[str, Any], int]] = []
    selected: list[dict[str, Any]] = []
    for row in plan:
        if scenario and row["scenario"] != scenario:
            continue
        if fault and row["problem"] != fault:
            continue
        if resource_class and effective_class(row) != resource_class:
            continue
        if shard and _shard_of(row, shard[1]) != shard[0]:
            continue
        if cases is not None and case_id(row) not in cases:
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
            if force or not result_current(row, repeat):
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
            result_current(row, repeat)
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
        "--shard",
        type=_parse_shard,
        help="Audit only cases in shard K of N (K/N), to split work across hosts",
    )
    parser.add_argument(
        "--cases",
        type=Path,
        help="File listing case names (one per line) to audit; other cases are skipped",
    )
    parser.add_argument(
        "--case-images",
        action="store_true",
        help="Audit the Firecracker case images trials restore (needs Firecracker)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Run on uncommitted edits; such records never count for a release report",
    )
    args = parser.parse_args()
    if args.case_images:
        os.environ[CASE_IMAGES_ENV] = "1"
    ok = run_matrix(
        jobs=args.jobs,
        retry_failed=args.retry_failed,
        scenario=args.scenario,
        fault=args.fault,
        resource_class=args.resource_class,
        force=args.force,
        repeats=args.repeats,
        allow_dirty=args.allow_dirty,
        shard=args.shard,
        cases=(
            {
                line.strip()
                for line in args.cases.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            }
            if args.cases
            else None
        ),
    )
    raise SystemExit(0 if ok else 1)
