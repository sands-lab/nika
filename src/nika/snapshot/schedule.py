"""Benchmark scheduling for ``nika.snapshot``: build first, group, admit by memory.

* :func:`prebuild_goldens` builds every golden VM a run needs before its trials
  start, several at a time, so trials only restore (and their case timeout
  never includes a build).
* :func:`group_by_variant` runs trials of one environment variant together.
* :func:`hugepage_need_mib` / :func:`hugepage_pool_mib` let the runner admit a
  trial only while the huge-page pool can hold its copy.
* :func:`clean_after_run` deletes the run's golden VMs if configured.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable

from nika.snapshot.config import snapshot_settings
from nika.snapshot.golden import (
    MEMORY,
    META,
    delete_golden,
    ensure_golden,
    golden_key,
    goldens_dir,
    list_goldens,
    variant_for_row,
    vm_memory_mib,
)

# Headroom over a golden memory file's data (memory the lab touches later).
HUGEPAGE_HEADROOM = 1.25


def _variant_id(row: dict[str, Any]) -> str:
    return json.dumps(variant_for_row(row), sort_keys=True)


def group_by_variant(trials: list[Any]) -> list[Any]:
    """Reorder trials so each variant's trials are adjacent (first-seen order)."""
    order: dict[str, int] = {}
    for trial in trials:
        order.setdefault(_variant_id(trial.row), len(order))
    return sorted(trials, key=lambda trial: order[_variant_id(trial.row)])


def build_parallelism(max_parallel: int) -> int:
    """Golden builds to run at once: limited by vCPUs and the huge-page pool."""
    settings = snapshot_settings()
    by_cpu = max(1, (os.cpu_count() or 1) // settings.vm_cpus)
    limit = min(max_parallel, by_cpu)
    if settings.huge_pages:
        # A build can fill its whole VM memory; size by the default VM.
        limit = min(limit, max(1, hugepage_pool_mib() // vm_memory_mib("")))
    return max(1, limit)


def prebuild_goldens(trials: Iterable[Any], *, max_parallel: int) -> list[str]:
    """Build the missing golden VMs of ``trials``; return failure messages."""
    variants: dict[str, dict[str, Any]] = {}
    for trial in trials:
        params = variant_for_row(trial.row)
        variants.setdefault(json.dumps(params, sort_keys=True), params)
    missing = [
        p
        for p in variants.values()
        if not (goldens_dir() / golden_key(p) / META).exists()
    ]
    if not missing:
        return []
    workers = min(len(missing), build_parallelism(max_parallel))
    print(
        f"[snapshot] building {len(missing)} golden VM(s) before the trials, "
        f"{workers} at a time",
        flush=True,
    )
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(ensure_golden, params): params for params in missing}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - its trials retry the build
                message = f"golden VM for {futures[future]} failed: {exc}"
                print(f"[snapshot] {message}", flush=True)
                failures.append(message)
    return failures


def hugepage_pool_mib() -> int:
    """Size of the host's huge-page pool (all reserved huge pages)."""
    info = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("HugePages_Total", "Hugepagesize"))
    }
    return info.get("HugePages_Total", 0) * info.get("Hugepagesize", 0) // 1024


def hugepage_need_mib(row: dict[str, Any]) -> int:
    """Huge pages a trial's copy is expected to use (0 without huge pages).

    A copy takes huge pages for the data in its golden memory file (which the
    page server prefetches; holes stay unused) plus what the lab touches later:
    the file's data size plus headroom, or the whole VM memory before the golden
    VM exists.
    """
    if not snapshot_settings().huge_pages:
        return 0
    params = variant_for_row(row)
    vm_mib = vm_memory_mib(str(params.get("scenario")))
    memory = goldens_dir() / golden_key(params) / MEMORY
    try:
        data_mib = memory.stat().st_blocks * 512 >> 20
    except OSError:
        return vm_mib
    return min(int(data_mib * HUGEPAGE_HEADROOM), vm_mib)


def clean_after_run(trials: Iterable[Any]) -> None:
    """Delete the golden VMs this run used when ``nika.snapshot.clean_after_run``."""
    if not snapshot_settings().clean_after_run:
        return
    keys = {golden_key(variant_for_row(trial.row)) for trial in trials}
    from nika.snapshot.trial import trial_rows

    in_use = {row["golden"] for row in trial_rows() if row["alive"]}
    removed = 0
    for row in list_goldens():
        if row["key"] in keys and row["key"] not in in_use and not row["building"]:
            delete_golden(row)
            removed += 1
    if removed:
        print(
            f"[snapshot] deleted {removed} golden VM(s) (clean_after_run)", flush=True
        )
