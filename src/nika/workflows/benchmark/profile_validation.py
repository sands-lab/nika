"""Execution-validated coverage of native router device profiles.

Declared compatibility (``ProblemBase.is_compatible`` with a device profile,
plus ``runs_on_device_profile``) says a failure *should* work on an IOS XR or
RouterOS deployment. This runner checks it: for each declared
(environment, device_profile, failure) cell in the working pool it runs one
representative case end to end — deploy, inject, ``verify_fault``, recovery
where the failure defines it, close — and records the outcome in a generated,
versioned results file. Run it on demand or nightly, not on every change:

    uv run nika benchmark validate-profiles --native-only

Cells run one at a time under the host-wide heavy-lab lock. Recorded cells are
skipped, so an interrupted run resumes; ``--rerun`` repeats them.
"""

from __future__ import annotations

import fcntl
import subprocess
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from typing import Any

import yaml

from nika.config import BENCHMARK_DIR, REPO_ROOT

RESULTS_PATH = BENCHMARK_DIR / "working" / "validation" / "device_profiles.yaml"
SCHEMA_VERSION = 1
HEAVY_LAB_LOCK = Path("/tmp/nika-heavy-lab.lock")

VALIDATED = "validated"
FAILED = "failed"
UNSUPPORTED = "unsupported"

# ISP base scenarios share one capability surface; validate the family once.
ISP_ENVIRONMENT = "isp"
_PREFERRED_ISP = "isp_abilene"


def cell_environment(scenario: str) -> str:
    """Environment key of a cell: the scenario, or ``isp`` for base ISP graphs."""
    from nika.workflows.benchmark.isp_options import is_isp_base_topology

    return ISP_ENVIRONMENT if is_isp_base_topology(scenario) else scenario


def cell_key(environment: str, device_profile: str, failure: str) -> str:
    return f"{environment}/{device_profile}/{failure}"


@dataclass(frozen=True)
class Cell:
    environment: str
    device_profile: str
    failure: str
    row: dict[str, Any]
    # Whether the representative targets a router running the native profile.
    native_target: bool

    @property
    def key(self) -> str:
        return cell_key(self.environment, self.device_profile, self.failure)


def _native_target(row: dict[str, Any], env_cache: dict) -> bool:
    from nika.problems.rca.inventory import load_offline_net_env
    from nika.problems.support.router_ops import native_router_profile
    from nika.workflows.benchmark.isp_options import ISP_OPTION_KEYS

    options = {key: row[key] for key in ISP_OPTION_KEYS if key in row}
    cache_key = (row["scenario"], row.get("topo_size", ""), *sorted(options.items()))
    if cache_key not in env_cache:
        env_cache[cache_key] = load_offline_net_env(
            row["scenario"], row.get("topo_size", ""), **options
        )
    env = env_cache[cache_key]
    nodes = [str(value) for value in row["inject"].values()]
    nodes += [rc["resource"].get("node") for rc in row.get("root_causes") or []]
    return any(node and native_router_profile(env, node) for node in nodes)


def declared_cells(pool: Path | None = None) -> list[Cell]:
    """Return one representative case per native-profile cell of the pool.

    Prefers a case whose target is a native router, so the cell exercises the
    vendor's fault operations rather than only the FRR/Linux nodes around it.
    """
    from nika.net_env.net_env_pool import scenario_device_profiles
    from nika.workflows.benchmark.generate import default_pool_dir
    from nika.workflows.benchmark.healthy import is_healthy_case
    from nika.workflows.benchmark.load_config import load_benchmark_input

    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for row in load_benchmark_input(pool or default_pool_dir()):
        profile = row.get("device_profile")
        if (
            is_healthy_case(row["problem"])
            or len(row.get("problems") or [row["problem"]]) > 1
            or profile not in scenario_device_profiles(row["scenario"])
        ):
            continue
        key = (cell_environment(row["scenario"]), profile, row["problem"])
        groups.setdefault(key, []).append(row)

    env_cache: dict = {}
    cells: list[Cell] = []
    for (environment, profile, failure), rows in sorted(groups.items()):
        ranked = sorted(
            rows,
            key=lambda r: (r["scenario"] != _PREFERRED_ISP, r["candidate_option_id"]),
        )
        chosen = next((r for r in ranked if _native_target(r, env_cache)), None)
        cells.append(
            Cell(
                environment,
                profile,
                failure,
                chosen or ranked[0],
                native_target=chosen is not None,
            )
        )
    return cells


def load_results(path: Path = RESULTS_PATH) -> dict[str, dict[str, Any]]:
    """Return recorded cell results keyed by ``environment/profile/failure``."""
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {
        cell_key(r["environment"], r["device_profile"], r["failure"]): r
        for r in data.get("cells") or []
    }


def write_results(
    records: dict[str, dict[str, Any]], path: Path = RESULTS_PATH
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "nika benchmark validate-profiles",
        "cells": [records[key] for key in sorted(records)],
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def prune_results(declared: Iterable[Cell], path: Path = RESULTS_PATH) -> list[str]:
    """Drop recorded cells the pool no longer declares; return their keys."""
    keys = {cell.key for cell in declared}
    with heavy_lab_lock():
        records = load_results(path)
        stale = sorted(set(records) - keys)
        if stale:
            write_results({k: r for k, r in records.items() if k in keys}, path)
    return stale


def validated_cells(path: Path = RESULTS_PATH) -> set[str]:
    return {key for key, r in load_results(path).items() if r["status"] == VALIDATED}


def profile_image(profile: str) -> str:
    """Vendor image tag a native profile deploys."""
    return import_module(f"nika.net_env.utils.{profile}.common").IMAGE


def _nika_commit() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


@contextmanager
def heavy_lab_lock(path: Path = HEAVY_LAB_LOCK) -> Iterator[None]:
    """Hold the host-wide heavy-lab lock (the one ``flock`` takes)."""
    with open(path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_cell(cell: Cell, *, result_dir: str | None = None) -> dict[str, Any]:
    """Deploy, inject, verify and recover one cell's case; always close the lab."""
    from nika.runtime.base import RuntimeCapabilityError
    from nika.validation.presence import bound_injected_problem
    from nika.workflows.benchmark.isp_options import ISP_OPTION_KEYS
    from nika.workflows.benchmark.inject_resolve import validate_benchmark_case
    from nika.workflows.benchmark.trials import task_id_for_row
    from nika.workflows.env.start import start_net_env
    from nika.workflows.failure.inject import inject_failure
    from nika.workflows.session.close import close_session
    from nika.net_env.net_env_pool import scenario_requires_topo_size

    row = cell.row
    record: dict[str, Any] = {
        "environment": cell.environment,
        "device_profile": cell.device_profile,
        "failure": cell.failure,
        "scenario": row["scenario"],
        "task_id": task_id_for_row(row),
        "inject": dict(row["inject"]),
        "native_target": cell.native_target,
        "nika_commit": _nika_commit(),
        "image": profile_image(cell.device_profile),
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    options = {key: row[key] for key in ISP_OPTION_KEYS if key in row}
    stage = "deploy"
    session_id = None
    started = time.monotonic()
    try:
        validate_benchmark_case(
            row["scenario"],
            row["problem"],
            row["inject"],
            row.get("topo_size", ""),
            isp_options=options,
        )
        size = (
            row.get("topo_size")
            if scenario_requires_topo_size(row["scenario"])
            else None
        )
        session_id = start_net_env(
            row["scenario"],
            size,
            instance_tag="validate",
            result_dir=result_dir,
            **options,
        )
        stage = "inject"
        # Raises unless verify_fault confirms the artifact and the ground
        # truth matches the materialized root causes.
        inject_failure(
            [row["problem"]],
            session_id=session_id,
            param_overrides=row["inject"],
            expected_root_causes=row.get("root_causes"),
        )
        stage = "recover"
        bound = bound_injected_problem(session_id)
        recovery = "not_defined"
        if bound is not None and hasattr(bound[0], "recover_fault"):
            result = bound[0].recover_fault(bound[1]) or {}
            if result.get("verified") is False:
                raise RuntimeError(f"recover_fault did not restore: {result}")
            recovery = "restored"
        record.update(status=VALIDATED, recovery=recovery)
    except RuntimeCapabilityError as exc:
        record.update(status=UNSUPPORTED, stage=stage, error=str(exc)[:500])
    except Exception as exc:  # noqa: BLE001 - recorded as the cell outcome
        record.update(status=FAILED, stage=stage, error=str(exc)[:500])
    finally:
        if session_id is not None:
            close_session(session_id=session_id)
    record["duration_sec"] = round(time.monotonic() - started)
    return record


def validate_cells(
    cells: Iterable[Cell],
    *,
    results_path: Path = RESULTS_PATH,
    rerun: bool = False,
    limit: int | None = None,
    result_dir: str | None = None,
    echo=print,
) -> dict[str, dict[str, Any]]:
    """Run pending cells one at a time and record each result as it lands."""
    from nika.net_env.utils.kathara.docker_files.docker_images import image_exists

    records = load_results(results_path)
    done = 0
    for cell in cells:
        if limit is not None and done >= limit:
            break
        from nika.workflows.benchmark.trials import task_id_for_row

        with heavy_lab_lock():
            # Another validator may have finished while this one waited.
            # Reload and persist under the same lock to keep both results.
            records = load_results(results_path)
            recorded = records.get(cell.key)
            if (
                recorded is not None
                and not rerun
                and recorded.get("task_id") == task_id_for_row(cell.row)
            ):
                continue
            image = profile_image(cell.device_profile)
            if not image_exists(image):
                echo(f"skip {cell.key}: image {image} is not installed")
                continue
            echo(f"run  {cell.key} ({cell.row['scenario']}, {cell.row['inject']})")
            record = run_cell(cell, result_dir=result_dir)
            records[cell.key] = record
            write_results(records, results_path)
        echo(f"     {record['status']} after {record['duration_sec']}s")
        done += 1
    return records
