"""Benchmark scheduling for Firecracker execution: build first, group, admit by memory.

* :func:`prebuild_images` builds every lab image (and, with ``case_images``,
  every case image) a run needs before its trials start, several at a time, so
  trials only restore (and their case timeout never includes a build).
* :func:`group_by_variant` runs trials of one environment variant together.
* :func:`hugepage_need_mib` / :func:`hugepage_pool_mib` let the runner admit a
  trial only while the free huge pages can hold its copy.
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
from nika.firecracker.vmm import kill_pid_file
from nika.firecracker.image import (
    META,
    build_space_mib,
    delete_image,
    ensure_image,
    free_root_mib,
    image_key,
    images_dir,
    list_images,
    variant_for_row,
    vm_memory_mib,
)


# Huge pages kept free beyond the admitted copies.
HUGEPAGE_MARGIN_MIB = 512


def _variant_id(row: dict[str, Any]) -> str:
    return json.dumps(variant_for_row(row), sort_keys=True)


def group_by_variant(trials: list[Any]) -> list[Any]:
    """Reorder trials so each variant's trials are adjacent (first-seen order)."""
    order: dict[str, int] = {}
    for trial in trials:
        order.setdefault(_variant_id(trial.row), len(order))
    return sorted(trials, key=lambda trial: order[_variant_id(trial.row)])


def build_parallelism(max_parallel: int) -> int:
    """Builds to run at once, by vCPUs (huge pages and disk: see _build_admitted)."""
    by_cpu = max(1, (os.cpu_count() or 1) // firecracker_settings().vm_cpus)
    return max(1, min(max_parallel, by_cpu))


def _build_mib(params: dict[str, Any]) -> int:
    """Huge pages a build holds: writing the lab image touches all VM memory."""
    if not firecracker_settings().huge_pages:
        return 0
    return vm_memory_mib(str(params.get("scenario")))


def _disk_mib(params: dict[str, Any]) -> int:
    return build_space_mib(str(params.get("scenario")))


def _build_admitted(
    items: list[Any],
    *,
    mib: Callable[[Any], int],
    disk: Callable[[Any], int],
    submit: Callable[[Any], Future],
    workers: int,
    label: Callable[[Any], str],
) -> None:
    """Run builds, at most ``workers`` and only while huge pages and root space
    hold them (each build checks only its own need, so parallel builds would
    all pass that check and then fill the root together)."""
    pool_mib = hugepage_pool_mib()
    pending = sorted(items, key=mib, reverse=True)
    running: dict[Future, Any] = {}
    while pending or running:
        for item in list(pending):
            if len(running) >= workers:
                break
            held = sum(mib(i) for i in running.values())
            if running and pool_mib is not None and held + mib(item) > pool_mib:
                continue
            reserved = sum(disk(i) for i in running.values())
            if running and reserved + disk(item) > free_root_mib():
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


def prebuild_images(trials: Iterable[Any], *, max_parallel: int) -> None:
    """Build the missing lab images (and case images) of ``trials``.

    A failed build is reported; the trials of its variant retry it.
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
    if missing:
        workers = min(len(missing), build_parallelism(max_parallel))
        print(
            f"[firecracker] building {len(missing)} lab image(s) before the trials, "
            f"{workers} at a time",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                _build_admitted(
                    missing,
                    mib=_build_mib,
                    disk=_disk_mib,
                    submit=lambda p: pool.submit(ensure_image, p),
                    workers=workers,
                    label=lambda p: f"lab image for {p}",
                )
            except BaseException:
                # Ctrl-C / SIGTERM: stop this run's build VMs, so the builder
                # threads fail at once instead of finishing their builds.
                pool.shutdown(wait=False, cancel_futures=True)
                _stop_own_builds()
                raise
    prebuild_cases(trials, max_parallel=max_parallel)


def _stop_own_builds() -> None:
    for build in images_dir().glob("*.build"):
        try:
            builder = json.loads((build / "builder.json").read_text())
        except (OSError, ValueError):
            continue
        if builder.get("pid") == os.getpid():
            kill_pid_file(build / "firecracker.pid")


def prebuild_cases(trials: Iterable[Any], *, max_parallel: int) -> None:
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
        return
    workers = min(len(missing), build_parallelism(max_parallel))
    print(
        f"[firecracker] building {len(missing)} case image(s) before the trials, "
        f"up to {workers} at a time",
        flush=True,
    )
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
        _build_admitted(
            missing,
            mib=lambda row: _build_mib(variant_for_row(row)),
            disk=lambda row: _disk_mib(variant_for_row(row)),
            submit=lambda row: pool.submit(build_case_worker, row),
            workers=workers,
            label=lambda row: (
                f"case image for {row['problem']} ({variant_for_row(row)})"
            ),
        )


def hugepage_pool_mib() -> int | None:
    """Free huge pages in MiB (None without huge pages).

    Read it before the run starts copies: pages that other runs, builds or VMs
    hold are not free, so admitting against the reserved total would let copies
    find the pool empty.
    """
    if not firecracker_settings().huge_pages:
        return None
    info = {
        line.split(":")[0]: int(line.split()[1])
        for line in Path("/proc/meminfo").read_text().splitlines()
        if line.startswith(("HugePages_Free", "Hugepagesize"))
    }
    free = info.get("HugePages_Free", 0) * info.get("Hugepagesize", 0) // 1024
    # Copies that fill the pool exactly leave no room for pages still being
    # released by VMs that just exited.
    return max(free - HUGEPAGE_MARGIN_MIB, 0)


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
