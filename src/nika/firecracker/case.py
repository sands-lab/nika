"""Case images: a lab image copy saved right after its case's failure injection.

With ``nika.firecracker.case_images``, each failure case is injected once:
restore its lab image, adopt the lab session, verify it, inject the case's
failure (the same ``inject_failure`` path as a trial), then snapshot the VM.
The case image in ``<root>/cases/<key>/`` keeps:

* ``mem.zdiff``: the memory pages that differ from the lab image, compressed
  (``nika.firecracker.memdiff``); restores overlay them on the lab image memory;
* ``vm.state`` and ``disk.img`` (a copy-on-write clone: only changed blocks);
* ``case.json``: the lab image key and the injected session's id.

Trials of the case restore it, adopt the injected session (its ground truth
moves with it) and skip verification and injection. The key covers the lab
image key and the case row, so a new lab image builds new case images.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nika.firecracker import memdiff
from nika.firecracker.config import firecracker_settings
from nika.firecracker.image import (
    MEMORY,
    STATE,
    LabImage,
    _clone,
    _key_lock,
    _log,
    _ready,
    ensure_image,
    image_key,
    require_build_space,
    root,
    variant_for_row,
    vm_memory_mib,
)
from nika.firecracker.vmm import DISK, Firecracker, firecracker_binary, pid_alive

ZDIFF = "mem.zdiff"
CASE_META = "case.json"


@dataclass(frozen=True)
class CaseImage:
    """A ready case image and the lab image its memory diff applies to."""

    key: str
    path: Path
    image: LabImage
    session_id: str


def cases_dir() -> Path:
    return root() / "cases"


def wants_case_image(row: dict[str, Any]) -> bool:
    """Case images are on and the row injects a failure."""
    from nika.workflows.benchmark.healthy import is_healthy_case

    return firecracker_settings().case_images and not is_healthy_case(
        row.get("problem")
    )


def case_key(row: dict[str, Any]) -> str:
    from nika.workflows.benchmark.resume import benchmark_row_fingerprint

    payload = {
        "image": image_key(variant_for_row(row)),
        "case": benchmark_row_fingerprint(row),
        "root_causes": row.get("root_causes"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def _ready_case(key: str) -> CaseImage | None:
    path = cases_dir() / key
    try:
        meta = json.loads((path / CASE_META).read_text())
    except FileNotFoundError:
        return None
    image = _ready(meta["image"])
    if image is None:
        return None
    return CaseImage(key=key, path=path, image=image, session_id=meta["session_id"])


def ensure_case(row: dict[str, Any]) -> CaseImage:
    """Return the case image of ``row``, building it (and its lab image) if needed."""
    ensure_image(variant_for_row(row))
    key = case_key(row)
    case = _ready_case(key)
    if case is not None:
        return case
    with _key_lock(f"case-{key}"):
        case = _ready_case(key)  # another process may have built it meanwhile
        if case is not None:
            return case
        _log(f"[firecracker] building case image {key} for {row['problem']}")
        started = time.monotonic()
        build = cases_dir() / f"{key}.build"
        if build.exists():
            shutil.rmtree(build)  # an interrupted build; we hold its lock
        require_build_space(vm_memory_mib(str(row["scenario"])))
        build.mkdir(parents=True)
        try:
            _build_case(build, key, row)
        except BaseException:
            shutil.rmtree(build, ignore_errors=True)
            raise
        os.replace(build, cases_dir() / key)
        _log(
            f"[firecracker] case image {key} ready in {time.monotonic() - started:.0f}s"
        )
        return _ready_case(key)  # type: ignore[return-value]


def _build_case(build: Path, key: str, row: dict[str, Any]) -> None:
    from nika.firecracker.trial import TrialLab
    from nika.utils.session_store import SessionStore
    from nika.workflows.benchmark.multi_fault import row_inject_args
    from nika.workflows.failure.inject import inject_failure

    (build / "builder.json").write_text(
        json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "row": row})
    )
    session_id = f"case-{key}"
    results = build / "results"
    problems, overrides, expected = row_inject_args(row)
    timings: dict[str, float] = {}
    with TrialLab(variant_for_row(row), trial_key=session_id, owner=str(build)) as lab:
        assert lab.image is not None
        lab.attach(
            session_id=session_id,
            result_dir=str(results),
            session_dir=str(results / session_id),
        )
        try:
            started = time.monotonic()
            inject_failure(
                problem_names=problems,
                session_id=session_id,
                param_overrides=overrides,
                expected_root_causes=expected,
            )
            timings["inject_sec"] = round(time.monotonic() - started, 1)
            started = time.monotonic()
            vm = Firecracker(lab.dir, firecracker_binary(firecracker_settings().binary))
            vm.snapshot("case.state", "case.mem")
            timings["snapshot_sec"] = round(time.monotonic() - started, 1)
            started = time.monotonic()
            sizes = memdiff.encode(
                lab.image.path / MEMORY, lab.dir / "case.mem", build / ZDIFF
            )
            timings["encode_sec"] = round(time.monotonic() - started, 1)
            os.replace(lab.dir / "case.state", build / STATE)
            _clone(lab.dir / DISK, build / DISK)
        finally:
            # The injected session lives on in the case image, not on this host.
            SessionStore().delete_session(session_id, status="finished")
        image = lab.image
    shutil.rmtree(results, ignore_errors=True)
    (build / "builder.json").unlink(missing_ok=True)
    (build / CASE_META).write_text(
        json.dumps(
            {
                "image": image.key,
                "session_id": session_id,
                "problem": row.get("problem"),
                "variant": variant_for_row(row),
                "diff_pages": sizes["pages"],
                "diff_bytes": sizes["bytes"],
                **timings,
            }
        )
    )


def build_case_worker(row: dict[str, Any]) -> str:
    """Spawn-process entry: build one case image (trial labs are per process)."""
    return ensure_case(row).key


def list_cases() -> list[dict[str, Any]]:
    from nika.firecracker.image import images_dir

    rows = []
    directory = cases_dir()
    if not directory.is_dir():
        return rows
    for path in sorted(directory.iterdir()):
        key = path.name.removesuffix(".build")
        if path.name.endswith(".build"):
            try:
                builder = json.loads((path / "builder.json").read_text())
            except (OSError, ValueError):
                builder = {}
            alive = builder.get("host") not in (None, socket.gethostname()) or (
                bool(builder.get("pid")) and pid_alive(int(builder["pid"]))
            )
            rows.append({"key": key, "path": path, "ready": False, "building": alive})
            continue
        try:
            meta = json.loads((path / CASE_META).read_text())
        except (OSError, ValueError):
            meta = {}
        image = meta.get("image", "")
        rows.append(
            {
                "key": key,
                "path": path,
                "ready": bool(meta),
                "building": False,
                "image": image,
                # A case image is usable while its lab image is current.
                "current": bool(meta) and _image_current(images_dir() / image),
                "problem": meta.get("problem"),
                "diff_bytes": meta.get("diff_bytes", 0),
            }
        )
    return rows


def _image_current(path: Path) -> bool:
    try:
        variant_ = json.loads((path / "meta.json").read_text()).get("variant", {})
    except (OSError, ValueError):
        return False
    return bool(variant_) and path.name == image_key(variant_)


def delete_case(row: dict[str, Any]) -> None:
    shutil.rmtree(row["path"], ignore_errors=True)
    (root() / "locks" / f"case-{row['key']}.lock").unlink(missing_ok=True)
