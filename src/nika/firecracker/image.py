"""LabImage VMs: one deployed, verified lab per environment variant and code version.

A lab image boots a copy of the packaged base image in Firecracker, receives
this checkout's code, deploys the environment through its NIKA remote daemon
(the same ``start_net_env`` path as a cold trial, including startup
verification), and is kept as a full snapshot: ``disk.img``, ``vm.state`` and
``mem`` in ``<root>/images/<key>/``. The key covers the deploy parameters,
lab settings, code, base image and VM shape, so any change builds a new image.

A build runs in ``<key>.build/`` and is renamed to ``<key>/`` when complete;
build locks live in ``<root>/locks/``.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import tarfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from nika.config import REPO_ROOT
from nika.firecracker.base import (
    GUEST_REPO,
    PROXY_PORT,
    SYNCED_FILES,
    _repo_files,
    ensure_base,
)
from nika.firecracker.binary import binary_id
from nika.firecracker.config import firecracker_root, firecracker_settings
from nika.firecracker.vmm import (
    DISK,
    VSOCK,
    Firecracker,
    HostProxy,
    VsockForwarder,
    firecracker_binary,
    guest_exec,
    kill_pid_file,
    pid_alive,
    wait_http_ok,
)

DAEMON_PORT = 8700
GATEWAY_PORT = 8800
STATE = "vm.state"
MEMORY = "mem"
META = "meta.json"
# Linux reports freed memory about 2 s after it is freed.
# Warn when a deployed lab leaves less than this share of its VM memory free.
LOW_MEMORY_FRACTION = 0.25
FREE_PAGE_REPORT_SEC = 5
# Deploy plus startup verification in a lab image build (longest seen: about
# 10 min for isp_pdh); a lab that cannot start (e.g. too little VM memory) must
# not hang the build.
BUILD_DEPLOY_TIMEOUT_SEC = 1800
# Free space kept on the Firecracker root beyond a build's VM memory.
BUILD_SPACE_MARGIN_MIB = 4096
# Deploy parameters that change the lab; mirrors ``start_net_env``'s keywords.
VARIANT_KEYS = (
    "scenario",
    "topo_size",
    "topo",
    "igp",
    "bgp_mode",
    "rpki",
    "backend",
    "device_profile",
)
# Paths whose content can change a deployed lab or the daemon serving it ...
FINGERPRINT_PATHS = ("src/", "pyproject.toml")
# ... minus code that only runs on the host: agents, LLM clients, evaluation and
# the result viewers. Changing them keeps the lab images.
HOST_ONLY_PATHS = (
    "src/agent/byo/",
    "src/agent/cli/",
    "src/agent/community/",
    "src/agent/llm/",
    "src/agent/sdk/",
    "src/agent/skills/",
    "src/nika/agents/mock/",
    "src/nika/agents/sandbox/manager.py",
    "src/nika/agents/sandbox/sbx/",
    "src/nika/agents/sandbox/e2b/",
    "src/nika/evaluator/",
    # Host side of Firecracker runs; base image and binary have their own key parts.
    "src/nika/firecracker/",
    "src/nika/inspect/",
    "src/nika/tui/",
)
# Run-config sections that shape a deploy; lab images deploy with the host's values.
LAB_SETTING_SECTIONS = ("lab", "runtime_validation", "static_validation", "k8s")


@dataclass(frozen=True)
class LabImage:
    """A ready lab image."""

    key: str
    path: Path
    session_id: str


def root() -> Path:
    return firecracker_root()


def images_dir() -> Path:
    return root() / "images"


def variant(**params: Any) -> dict[str, Any]:
    """Deploy parameters of one environment variant (``None`` values dropped)."""
    return {key: params.get(key) for key in VARIANT_KEYS if params.get(key) is not None}


def variant_for_row(row: dict[str, Any]) -> dict[str, Any]:
    """Environment variant a benchmark case row deploys."""
    from nika.net_env.net_env_pool import scenario_requires_topo_size
    from nika.workflows.benchmark.isp_options import ISP_DEPLOY_KEYS

    scenario = row["scenario"]
    size = row.get("topo_size") or None
    if not scenario_requires_topo_size(scenario):
        size = None
    return variant(
        scenario=scenario,
        topo_size=size,
        **{key: row.get(key) for key in ISP_DEPLOY_KEYS},
    )


@functools.cache
def code_fingerprint() -> str:
    """Hash of the code a lab image runs (``FINGERPRINT_PATHS`` of this checkout)."""
    files = [
        f
        for f in _repo_files(REPO_ROOT)
        if f.startswith(FINGERPRINT_PATHS) and not f.startswith(HOST_ONLY_PATHS)
    ]
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode() + b"\0")
        digest.update(hashlib.sha256((REPO_ROOT / rel).read_bytes()).digest())
    return digest.hexdigest()[:16]


def lab_settings() -> dict[str, Any]:
    from nika.run_config.loader import get_run_config

    nika = get_run_config().nika.model_dump(mode="json")
    return {section: nika[section] for section in LAB_SETTING_SECTIONS}


def vm_memory_mib(scenario: str) -> int:
    settings = firecracker_settings()
    memory = settings.vm_memory_overrides.get(scenario, settings.vm_memory)
    return int(memory[:-3]) * 1024


def base_manifest() -> dict[str, Any]:
    """The current base image's manifest, or ``{}`` before the first build."""
    try:
        return json.loads((root() / "base" / "base.json").read_text())
    except (OSError, ValueError):
        return {}


def image_key(params: dict[str, Any]) -> str:
    settings = firecracker_settings()
    payload = {
        "variant": params,
        "lab_settings": lab_settings(),
        "code": code_fingerprint(),
        "base": base_manifest().get("id"),
        "cpus": settings.vm_cpus,
        "memory_mib": vm_memory_mib(str(params.get("scenario"))),
        "huge_pages": settings.huge_pages,
        "firecracker": binary_id(firecracker_binary()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


@contextmanager
def _key_lock(key: str) -> Iterator[None]:
    """Serialize builds of one lab image across processes."""
    locks = root() / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    with open(locks / f"{key}.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _ready(key: str) -> LabImage | None:
    path = images_dir() / key
    try:
        meta = json.loads((path / META).read_text())
    except FileNotFoundError:
        return None
    return LabImage(key=key, path=path, session_id=meta["session_id"])


def _log(message: str) -> None:
    print(message, flush=True)  # spawn workers: show build progress as it happens


def ensure_image(params: dict[str, Any]) -> LabImage:
    """Return the lab image for ``params``, building it first if needed."""
    image = _ready(image_key(params))
    if image is not None:
        return image
    ensure_base()  # first run, or the runtime images changed: (re)build the base
    key = image_key(params)
    image = _ready(key)
    if image is not None:
        return image
    with _key_lock(key):
        image = _ready(key)  # another trial may have built it meanwhile
        if image is not None:
            return image
        _log(f"[firecracker] building lab image {key} for {params}")
        started = time.monotonic()
        build = images_dir() / f"{key}.build"
        try:
            _build(build, key, params)
        finally:
            kill_pid_file(build / "firecracker.pid")
        (build / "firecracker.pid").unlink(missing_ok=True)
        os.replace(build, images_dir() / key)
        _log(
            f"[firecracker] lab image {key} ready in {time.monotonic() - started:.0f}s"
        )
        return _ready(key)  # type: ignore[return-value]


def require_build_space(memory_mib: int) -> None:
    """Fail before a build that could fill the Firecracker root.

    A snapshot writes the whole VM memory before its unused part becomes holes,
    and a full btrfs root fails every build and trial (and the run's own logs
    when they live there).
    """
    stat = os.statvfs(root())
    free_mib = stat.f_bavail * stat.f_frsize >> 20
    need_mib = memory_mib + BUILD_SPACE_MARGIN_MIB
    if free_mib < need_mib:
        raise RuntimeError(
            f"Only {free_mib / 1024:.0f} GiB free on {root()}; a build needs about "
            f"{need_mib / 1024:.0f} GiB. Delete stale lab and case images with "
            "`nika firecracker clean` (or free space) and run again."
        )


def _clone(src: Path, dst: Path) -> None:
    """Copy-on-write copy (the Firecracker root must support reflinks)."""
    done = subprocess.run(
        ["cp", "--reflink=always", str(src), str(dst)], capture_output=True, text=True
    )
    if done.returncode != 0:
        raise RuntimeError(
            f"Cannot clone {src} ({done.stderr.strip()}). The Firecracker root "
            f"{root()} must be on btrfs or XFS; see 'Set up the host' in "
            "docs/operations/firecracker.md."
        )


def _build(build: Path, key: str, params: dict[str, Any]) -> None:
    settings = firecracker_settings()
    base = root() / "base"
    manifest = base_manifest()
    if build.exists():
        # An interrupted build (we hold its lock): stop its VM before deleting it.
        kill_pid_file(build / "firecracker.pid")
        shutil.rmtree(build)
    require_build_space(vm_memory_mib(str(params.get("scenario"))))
    build.mkdir(parents=True)
    (build / "builder.json").write_text(
        json.dumps(
            {"pid": os.getpid(), "host": socket.gethostname(), "variant": params}
        )
    )
    _clone(base / manifest["rootfs"], build / DISK)
    vm = Firecracker(build, firecracker_binary())
    vm.start()
    vm.boot(
        kernel=str(base / manifest["kernel"]),
        initrd=str(base / manifest["initrd"]) if manifest.get("initrd") else None,
        boot_args=manifest["boot_args"],
        cpus=settings.vm_cpus,
        memory_mib=vm_memory_mib(str(params["scenario"])),
        huge_pages=settings.huge_pages,
        free_page_reporting=not settings.huge_pages,
    )
    uds = build / VSOCK
    wait_http_ok(uds, DAEMON_PORT)
    write_guest_lab_settings(uds)
    sync_code(uds)
    wait_http_ok(uds, DAEMON_PORT)

    from nika.remote.client import RemoteClient
    from nika.remote.config import RemoteConfig
    from nika.remote.protocol import EnvStartRequest

    session_id = f"image-{key}"
    forwarder = VsockForwarder(uds, DAEMON_PORT)
    try:
        client = RemoteClient(
            RemoteConfig(
                enabled=True, url=f"http://127.0.0.1:{forwarder.port}", artifact_root=""
            )
        )
        client.env_start(  # deploys and runs the startup verification
            EnvStartRequest(
                **params,
                session_id=session_id,
                static_validation=lab_settings()["static_validation"]["enabled"],
            ),
            timeout=BUILD_DEPLOY_TIMEOUT_SEC,
        )
    finally:
        forwarder.close()
    guest_memory = _guest_memory_mib(uds)
    if guest_memory["available"] < guest_memory["total"] * LOW_MEMORY_FRACTION:
        _log(
            f"[firecracker] warning: {params['scenario']} leaves only "
            f"{guest_memory['available']} of {guest_memory['total']} MiB free in its VM; "
            "raise nika.execution.firecracker.vm_memory_overrides for it"
        )
    if not settings.huge_pages:
        # Free the page cache and give the guest time to report it free, so the
        # memory file holds only memory the lab uses (and restores fault less).
        guest_exec(uds, "sync; echo 3 > /proc/sys/vm/drop_caches")
        time.sleep(FREE_PAGE_REPORT_SEC)
    vm.snapshot(STATE, MEMORY)
    vm.kill()
    # Zero pages (never used, or reported free) take no space.
    subprocess.run(["fallocate", "--dig-holes", str(build / MEMORY)], check=True)
    for name in (VSOCK, "api.sock"):
        (build / name).unlink(missing_ok=True)
    (build / META).write_text(
        json.dumps(
            {
                "session_id": session_id,
                "variant": params,
                "created": time.time(),
                "base": manifest["id"],
                "guest_memory_mib": guest_memory,
            }
        )
    )


def _guest_memory_mib(uds: Path) -> dict[str, int]:
    """Guest memory after the deploy: total and available (reclaimable included)."""
    fields = dict(
        line.split(":")
        for line in guest_exec(uds, "grep -E '^(MemTotal|MemAvailable):' /proc/meminfo")
        .strip()
        .splitlines()
    )
    total = int(fields["MemTotal"].split()[0]) // 1024
    available = int(fields["MemAvailable"].split()[0]) // 1024
    return {"total": total, "available": available, "used": total - available}


def write_guest_lab_settings(uds: Path) -> None:
    """Copy the host's lab deploy settings into the guest's config/nika.yaml.

    The guest's file comes from the base image; sections the synced code no
    longer knows are dropped, or the daemon would reject the whole file.
    """
    import yaml

    from nika.run_config.schema import NikaSettings, RunConfig

    path = f"{GUEST_REPO}/config/nika.yaml"
    config = yaml.safe_load(guest_exec(uds, f"cat {path}")) or {}
    config = {k: v for k, v in config.items() if k in RunConfig.model_fields}
    nika = {
        k: v
        for k, v in (config.get("nika") or {}).items()
        if k in NikaSettings.model_fields
    }
    config["nika"] = {**nika, **lab_settings()}
    guest_exec(
        uds,
        f"cat > {path} && chown ubuntu:ubuntu {path}",
        stdin=yaml.safe_dump(config, sort_keys=False).encode(),
    )


def sync_code(uds: Path, repo: Path = REPO_ROOT) -> None:
    """Make the guest checkout match this one, update deps, restart the daemon.

    ``.nika-files`` in the guest checkout lists the files of the last sync (the
    base image writes the first one), so files deleted here are deleted there.
    """
    files = _repo_files(repo)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in files:
            tar.add(repo / rel, arcname=rel)
    listed = guest_exec(uds, f"cat {GUEST_REPO}/{SYNCED_FILES} 2>/dev/null || true")
    stale = sorted(set(p for p in listed.split("\0") if p.strip()) - set(files))
    guest_exec(uds, f"su ubuntu -c 'tar -xf - -C {GUEST_REPO}'", stdin=buf.getvalue())
    if stale:
        guest_exec(
            uds, f"cd {GUEST_REPO} && xargs -0 rm -f", stdin="\0".join(stale).encode()
        )
    guest_exec(
        uds,
        f"cat > {GUEST_REPO}/{SYNCED_FILES} && chown ubuntu:ubuntu {GUEST_REPO}/{SYNCED_FILES}",
        stdin="\0".join(files).encode(),
    )
    # LabImage VMs have no network; dependencies come through a host proxy.
    proxy = HostProxy(uds.parent, PROXY_PORT)
    try:
        guest_exec(
            uds,
            proxy.guest_env()
            + f"cd {GUEST_REPO} && su ubuntu -c 'HTTPS_PROXY=$HTTPS_PROXY .venv/bin/uv sync -q'",
        )
    finally:
        proxy.close()
    guest_exec(uds, "sync && systemctl restart nika-remote")


def list_images() -> list[dict[str, Any]]:
    rows = []
    directory = images_dir()
    if not directory.is_dir():
        return rows
    for path in sorted(directory.iterdir()):
        building = path.name.endswith(".build")
        key = path.name.removesuffix(".build")
        if building:
            try:
                builder = json.loads((path / "builder.json").read_text())
            except (OSError, ValueError):
                builder = {}
            rows.append(
                {
                    "key": key,
                    "path": path,
                    "ready": False,
                    "building": _builder_alive(builder),
                    "current": False,
                    "variant": builder.get("variant", {}),
                }
            )
            continue
        try:
            meta = json.loads((path / META).read_text())
        except (OSError, ValueError):
            meta = {}
        variant_ = meta.get("variant", {})
        rows.append(
            {
                "key": key,
                "path": path,
                "ready": bool(meta),
                "building": False,
                # Current = what a trial would use now (the key recomputed today matches).
                "current": bool(variant_) and key == image_key(variant_),
                "variant": variant_,
                "guest_memory_mib": meta.get("guest_memory_mib", {}),
            }
        )
    return rows


def _builder_alive(builder: dict[str, Any]) -> bool:
    if builder.get("host") not in (None, socket.gethostname()):
        return True  # another host's build: assume it is alive
    return bool(builder.get("pid")) and pid_alive(int(builder["pid"]))


def delete_image(row: dict[str, Any]) -> None:
    kill_pid_file(Path(row["path"]) / "firecracker.pid")
    shutil.rmtree(row["path"], ignore_errors=True)
    (root() / "locks" / f"{row['key']}.lock").unlink(missing_ok=True)
