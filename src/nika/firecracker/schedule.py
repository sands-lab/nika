"""Benchmark scheduling for ``nika.firecracker``: build first, group, admit by memory.

* :func:`prebuild_images` builds every lab image a run needs before its trials
  start, several at a time, so trials only restore (and their case timeout
  never includes a build).
* :func:`group_by_variant` runs trials of one environment variant together.
* :func:`hugepage_need_mib` / :func:`hugepage_pool_mib` let the runner admit a
  trial only while the huge-page pool can hold its copy.
* :func:`clean_after_run` deletes the run's lab images if configured.
"""

from __future__ import annotations

import json
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Iterable

from nika.firecracker.base import ensure_base
from nika.firecracker.config import firecracker_settings
from nika.firecracker.image import (
    MEMORY,
    META,
    delete_image,
    ensure_image,
    image_key,
    images_dir,
    list_images,
    variant_for_row,
    vm_memory_mib,
)

# Headroom over a lab image memory file's data (memory the lab touches later).
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
    """LabImage builds to run at once, by vCPUs (huge pages: see prebuild_images)."""
    by_cpu = max(1, (os.cpu_count() or 1) // firecracker_settings().vm_cpus)
    return max(1, min(max_parallel, by_cpu))


def _build_mib(params: dict[str, Any]) -> int:
    """Huge pages a build holds: writing the lab image touches all VM memory."""
    if not firecracker_settings().huge_pages:
        return 0
    return vm_memory_mib(str(params.get("scenario")))


def prebuild_images(trials: Iterable[Any], *, max_parallel: int) -> list[str]:
    """Build the missing lab images of ``trials``; return failure messages."""
    ensure_base()  # build or refresh it before keys (which include its id)
    variants: dict[str, dict[str, Any]] = {}
    for trial in trials:
        params = variant_for_row(trial.row)
        variants.setdefault(json.dumps(params, sort_keys=True), params)
    missing = [
        p
        for p in variants.values()
        if not (images_dir() / image_key(p) / META).exists()
    ]
    if not missing:
        return []
    workers = min(len(missing), build_parallelism(max_parallel))
    print(
        f"[firecracker] building {len(missing)} lab image(s) before the trials, "
        f"{workers} at a time",
        flush=True,
    )
    failures = []
    pool_mib = hugepage_pool_mib() if firecracker_settings().huge_pages else 0
    pending = sorted(missing, key=_build_mib, reverse=True)
    running: dict[Any, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        while pending or running:
            # Admit builds while the huge-page pool holds their whole VM memory.
            for params in list(pending):
                if len(running) >= workers:
                    break
                held = sum(_build_mib(p) for p in running.values())
                if running and held + _build_mib(params) > pool_mib > 0:
                    continue
                pending.remove(params)
                running[pool.submit(ensure_image, params)] = params
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                params = running.pop(future)
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 - its trials retry the build
                    message = f"lab image for {params} failed: {exc}"
                    print(f"[firecracker] {message}", flush=True)
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

    A copy takes huge pages for the data in its lab image memory file (which the
    page server prefetches; holes stay unused) plus what the lab touches later:
    the file's data size plus headroom, or the whole VM memory before the lab image
    VM exists.
    """
    if not firecracker_settings().huge_pages:
        return 0
    params = variant_for_row(row)
    vm_mib = vm_memory_mib(str(params.get("scenario")))
    memory = images_dir() / image_key(params) / MEMORY
    try:
        data_mib = memory.stat().st_blocks * 512 >> 20
    except OSError:
        return vm_mib
    return min(int(data_mib * HUGEPAGE_HEADROOM), vm_mib)


def clean_after_run(trials: Iterable[Any]) -> None:
    """Delete the lab images this run used when ``nika.firecracker.clean_after_run``."""
    if not firecracker_settings().clean_after_run:
        return
    keys = {image_key(variant_for_row(trial.row)) for trial in trials}
    from nika.firecracker.trial import trial_rows

    in_use = {row["image"] for row in trial_rows() if row["alive"]}
    removed = 0
    for row in list_images():
        if row["key"] in keys and row["key"] not in in_use and not row["building"]:
            delete_image(row)
            removed += 1
    if removed:
        print(
            f"[firecracker] deleted {removed} lab image(s) (clean_after_run)",
            flush=True,
        )
