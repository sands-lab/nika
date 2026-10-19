"""Benchmark scheduling for ``nika.firecracker``: build first, group, admit by memory.

* :func:`prebuild_images` builds every lab image (and, with ``case_images``,
  every case image) a run needs before its trials start, several at a time, so
  trials only restore (and their case timeout never includes a build).
* :func:`group_by_variant` runs trials of one environment variant together.
* :func:`hugepage_need_mib` / :func:`hugepage_pool_mib` let the runner admit a
  trial only while the huge-page pool can hold its copy.
* :func:`clean_after_run` deletes the run's lab and case images if configured.
"""

from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import (
    FIRST_COMPLETED,
    Future,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    wait,
)
from pathlib import Path
from typing import Any, Callable, Iterable

from nika.firecracker.base import ensure_base
from nika.firecracker.case import (
    CASE_META,
    build_case_worker,
    case_key,
    cases_dir,
    delete_case,
    list_cases,
    wants_case_image,
)
from nika.firecracker.config import firecracker_settings
from nika.firecracker.image import (
    META,
    delete_image,
    ensure_image,
    image_key,
    images_dir,
    list_images,
    variant_for_row,
    vm_memory_mib,
)


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


def _build_admitted(
    items: list[Any],
    *,
    mib: Callable[[Any], int],
    submit: Callable[[Any], Future],
    workers: int,
    label: Callable[[Any], str],
) -> list[str]:
    """Run builds, at most ``workers`` and only while huge pages hold their VMs."""
    failures = []
    pool_mib = hugepage_pool_mib() if firecracker_settings().huge_pages else 0
    pending = sorted(items, key=mib, reverse=True)
    running: dict[Future, Any] = {}
    while pending or running:
        for item in list(pending):
            if len(running) >= workers:
                break
            held = sum(mib(i) for i in running.values())
            if running and held + mib(item) > pool_mib > 0:
                continue
            pending.remove(item)
            running[submit(item)] = item
        done, _ = wait(running, return_when=FIRST_COMPLETED)
        for future in done:
            item = running.pop(future)
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 - its trials retry the build
                message = f"{label(item)} failed: {type(exc).__name__}: {exc}"
                # A build process's traceback arrives as the exception's cause.
                where = f"\n{exc.__cause__}" if exc.__cause__ else ""
                print(f"[firecracker] {message}{where}", flush=True)
                failures.append(message)
    return failures


def prebuild_images(trials: Iterable[Any], *, max_parallel: int) -> list[str]:
    """Build the missing lab images (and case images) of ``trials``.

    Returns failure messages; the trials of a failed build retry it.
    """
    trials = list(trials)
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
    failures = []
    if missing:
        workers = min(len(missing), build_parallelism(max_parallel))
        print(
            f"[firecracker] building {len(missing)} lab image(s) before the trials, "
            f"{workers} at a time",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            failures += _build_admitted(
                missing,
                mib=_build_mib,
                submit=lambda p: pool.submit(ensure_image, p),
                workers=workers,
                label=lambda p: f"lab image for {p}",
            )
    return failures + prebuild_cases(trials, max_parallel=max_parallel)


def prebuild_cases(trials: Iterable[Any], *, max_parallel: int) -> list[str]:
    """Build the missing case images of ``trials`` (``case_images`` only).

    Each build runs in its own process: a process routes lab operations to one
    VM copy at a time.
    """
    rows: dict[str, dict[str, Any]] = {}
    for trial in trials:
        if wants_case_image(trial.row):
            rows.setdefault(case_key(trial.row), trial.row)
    missing = [
        row for key, row in rows.items() if not (cases_dir() / key / CASE_META).exists()
    ]
    if not missing:
        return []
    workers = min(len(missing), build_parallelism(max_parallel))
    print(
        f"[firecracker] building {len(missing)} case image(s) before the trials, "
        f"up to {workers} at a time",
        flush=True,
    )
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        return _build_admitted(
            missing,
            mib=lambda row: _build_mib(variant_for_row(row)),
            submit=lambda row: pool.submit(build_case_worker, row),
            workers=workers,
            label=lambda row: (
                f"case image for {row['problem']} ({variant_for_row(row)})"
            ),
        )


def hugepage_pool_mib() -> int:
    """Size of the host's huge-page pool (all reserved huge pages)."""
    info = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("HugePages_Total", "Hugepagesize"))
    }
    return info.get("HugePages_Total", 0) * info.get("Hugepagesize", 0) // 1024


def hugepage_need_mib(row: dict[str, Any]) -> int:
    """Huge pages to reserve for a trial's copy (0 without huge pages).

    A copy starts with the lab image's memory but can grow to its whole VM
    memory, and a VM that finds the pool empty crashes, so reserve all of it.
    """
    if not firecracker_settings().huge_pages:
        return 0
    return vm_memory_mib(str(variant_for_row(row).get("scenario")))


def clean_after_run(trials: Iterable[Any]) -> None:
    """Delete the lab and case images this run used when ``clean_after_run`` is set."""
    if not firecracker_settings().clean_after_run:
        return
    trials = list(trials)
    keys = {image_key(variant_for_row(trial.row)) for trial in trials}
    cases = {case_key(t.row) for t in trials if wants_case_image(t.row)}
    from nika.firecracker.trial import trial_rows

    alive = [row for row in trial_rows() if row["alive"]]
    in_use = {row["image"] for row in alive} | {row["case"] for row in alive}
    removed = 0
    for row in list_cases():
        if row["key"] in cases and row["key"] not in in_use and not row["building"]:
            delete_case(row)
            removed += 1
    for row in list_images():
        if row["key"] in keys and row["key"] not in in_use and not row["building"]:
            delete_image(row)
            removed += 1
    if removed:
        print(
            f"[firecracker] deleted {removed} lab/case image(s) (clean_after_run)",
            flush=True,
        )
