"""Golden VMs: one deployed, verified lab per environment variant and code version.

A golden VM is a copy of the packaged base VM. It receives this checkout's code,
deploys the environment through its NIKA remote daemon (the same
``start_net_env`` path as a cold trial, including startup verification), and
keeps the result in the stateful snapshot ``golden``. Its name encodes a key
over the deploy parameters, the code and the VM shape, so a code change builds
a new golden VM instead of reusing a stale lab.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import io
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from nika.config import REPO_ROOT
from nika.snapshot.config import snapshot_settings
from nika.snapshot.incus import (
    config_get,
    guest,
    incus,
    instance_exists,
    list_instances,
    require_incus,
    snapshot_names,
)
from nika.snapshot.vsock import VsockForwarder, wait_http_ok

GOLDEN_PREFIX = "nika-g-"
GOLDEN_SNAPSHOT = "golden"
DAEMON_PORT = 8700
GATEWAY_PORT = 8800
GUEST_REPO = "/home/ubuntu/nika"
# Host-wide: golden VMs belong to the Incus host, so every checkout must share
# the build locks.
LOCK_DIR = Path(tempfile.gettempdir()) / "nika-snapshot-locks"
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


@dataclass(frozen=True)
class Golden:
    """A ready golden VM."""

    name: str
    session_id: str


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


# Paths whose content can change a deployed lab or the daemon serving it.
FINGERPRINT_PATHS = ("src/", "pyproject.toml")


@functools.cache
def code_fingerprint() -> str:
    """Hash of the code a golden VM runs (``FINGERPRINT_PATHS`` of this checkout)."""
    files = [f for f in _repo_files(REPO_ROOT) if f.startswith(FINGERPRINT_PATHS)]
    digest = hashlib.sha256()
    for rel in files:
        digest.update(rel.encode() + b"\0")
        path = REPO_ROOT / rel
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()[:16]


def _repo_files(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-co", "--exclude-standard", "-z"],
        cwd=repo,
        capture_output=True,
        check=True,
    ).stdout
    return sorted(p for p in out.decode().split("\0") if p and (repo / p).is_file())


# Run-config sections that shape a deploy; golden VMs deploy with the host's values.
LAB_SETTING_SECTIONS = ("lab", "runtime_validation", "static_validation", "k8s")


def lab_settings() -> dict[str, Any]:
    from nika.run_config.loader import get_run_config

    nika = get_run_config().nika.model_dump(mode="json")
    return {section: nika[section] for section in LAB_SETTING_SECTIONS}


def vm_memory(scenario: str) -> str:
    settings = snapshot_settings()
    return settings.vm_memory_overrides.get(scenario, settings.vm_memory)


def golden_key(params: dict[str, Any]) -> str:
    settings = snapshot_settings()
    payload = {
        "variant": params,
        "lab_settings": lab_settings(),
        "code": code_fingerprint(),
        "base": settings.base,
        "cpus": settings.vm_cpus,
        "memory": vm_memory(str(params.get("scenario"))),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:12]


def _lock_dir() -> Path:
    LOCK_DIR.mkdir(exist_ok=True)
    try:
        LOCK_DIR.chmod(0o1777)  # shared by every user of the Incus host
    except PermissionError:
        pass  # another user created it
    return LOCK_DIR


@contextmanager
def _key_lock(key: str) -> Iterator[None]:
    """Serialize builds of one golden VM across processes and checkouts."""
    path = _lock_dir() / f"{key}.lock"
    while True:
        fd = os.open(path, os.O_RDONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            same = os.fstat(fd).st_ino == os.stat(path).st_ino
        except FileNotFoundError:
            same = False
        if same:
            break
        os.close(fd)  # pruned while we waited: lock the current file instead
    try:
        yield
    finally:
        os.close(fd)


def _ready(name: str) -> Golden | None:
    if not instance_exists(name) or GOLDEN_SNAPSHOT not in snapshot_names(name):
        return None
    if config_get(name, "user.nika.ready") != "true":
        return None
    return Golden(name=name, session_id=config_get(name, "user.nika.session_id"))


def _log(message: str) -> None:
    print(message, flush=True)  # spawn workers: show build progress as it happens


def ensure_golden(params: dict[str, Any]) -> Golden:
    """Return the golden VM for ``params``, building it first if needed."""
    require_incus()
    key = golden_key(params)
    name = f"{GOLDEN_PREFIX}{key}"
    golden = _ready(name)
    if golden is not None:
        return golden
    with _key_lock(key):
        golden = _ready(name)  # another trial may have built it meanwhile
        if golden is not None:
            return golden
        _log(f"[snapshot] building golden VM {name} for {params}")
        started = time.monotonic()
        try:
            golden = _build(name, key, params)
        except BaseException:
            # Do not leave a half-built VM running; the next trial rebuilds it.
            incus("delete", "--force", name, check=False)
            raise
        _log(f"[snapshot] golden VM {name} ready in {time.monotonic() - started:.0f}s")
        return golden


def _build(name: str, key: str, params: dict[str, Any]) -> Golden:
    settings = snapshot_settings()
    if instance_exists(name):
        incus("delete", "--force", name)  # leftover of an interrupted build
    memory = vm_memory(str(params["scenario"]))
    state = f"{int(memory[:-3]) + 2}GiB"
    config = {
        "limits.cpu": settings.vm_cpus,
        "limits.memory": memory,
        "user.nika.ready": "false",
        # Lets `nika snapshot clean` tell a build in progress from an abandoned
        # one; set with the copy so the VM never exists without them.
        "user.nika.builder_pid": os.getpid(),
        "user.nika.host": socket.gethostname(),
        "user.nika.variant": json.dumps(params, sort_keys=True),
    }
    incus(
        "copy",
        settings.base,
        name,
        *(arg for item in config.items() for arg in ("-c", "{}={}".format(*item))),
    )
    # The packaged base defines its own root disk; otherwise it comes from a
    # profile and needs an instance-level override.
    incus("config", "device", "set", name, "root", f"size.state={state}", check=False)
    if not _has_state_size(name, state):
        incus("config", "device", "override", name, "root", f"size.state={state}")
    incus("start", name)
    cid = int(config_get(name, "volatile.vsock_id"))
    wait_http_ok(cid, DAEMON_PORT)
    wait_agent(name)
    write_guest_lab_settings(name)
    sync_code(name)
    wait_http_ok(cid, DAEMON_PORT)

    from nika.remote.client import RemoteClient
    from nika.remote.config import RemoteConfig
    from nika.remote.protocol import EnvStartRequest

    session_id = f"golden-{key}"
    forwarder = VsockForwarder(cid, DAEMON_PORT)
    try:
        client = RemoteClient(
            RemoteConfig(
                enabled=True, url=f"http://127.0.0.1:{forwarder.port}", artifact_root=""
            )
        )
        request = EnvStartRequest(
            **params,
            session_id=session_id,
            static_validation=lab_settings()["static_validation"]["enabled"],
        )
        client.env_start(request)  # deploys and runs the startup verification
    finally:
        forwarder.close()
    # Freed pages are zero (init_on_free), so dropping the cache shrinks the state.
    guest(name, "sync; echo 3 > /proc/sys/vm/drop_caches")
    incus("snapshot", "create", name, GOLDEN_SNAPSHOT, "--stateful")
    incus(
        "config",
        "set",
        name,
        f"user.nika.session_id={session_id}",
        "user.nika.ready=true",
    )
    incus("stop", "--force", name)
    golden = _ready(name)
    assert golden is not None
    return golden


def _has_state_size(name: str, state: str) -> bool:
    return (
        incus(
            "config", "device", "get", name, "root", "size.state", check=False
        ).strip()
        == state
    )


def wait_agent(name: str, timeout: float = 300) -> None:
    """Wait until ``incus exec`` works (the guest agent is up)."""
    cmd = ["incus", "exec", name, "--", "true"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if subprocess.run(cmd, capture_output=True, check=False).returncode == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"incus agent in {name} not ready")


def write_guest_lab_settings(name: str) -> None:
    """Copy the host's lab deploy settings into the guest's config/nika.yaml."""
    import yaml

    path = f"{GUEST_REPO}/config/nika.yaml"
    config = yaml.safe_load(guest(name, f"cat {path}")) or {}
    config.setdefault("nika", {}).update(lab_settings())
    guest(
        name,
        f"su - ubuntu -c 'cat > {path}'",
        stdin=yaml.safe_dump(config, sort_keys=False).encode(),
    )


def sync_code(name: str, repo: Path = REPO_ROOT) -> None:
    """Make the guest checkout match this one, update deps, restart the daemon."""
    files = _repo_files(repo)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for rel in files:
            tar.add(repo / rel, arcname=rel)
    guest(name, f"su - ubuntu -c 'tar -xf - -C {GUEST_REPO}'", stdin=buf.getvalue())
    listed = guest(
        name,
        f"su - ubuntu -c 'cd {GUEST_REPO} && git ls-files -co --exclude-standard -z'",
    ).split("\0")
    stale = sorted(set(p for p in listed if p) - set(files))
    if stale:
        guest(
            name,
            f"su - ubuntu -c 'cd {GUEST_REPO} && xargs -0 rm -f'",
            stdin="\0".join(stale).encode(),
        )
    guest(
        name, f"su - ubuntu -c 'cd {GUEST_REPO} && .venv/bin/uv sync -q'", timeout=1800
    )
    # A stateful snapshot keeps the page cache, but later stateless copies of
    # this disk must see the new files.
    guest(name, "sync && systemctl restart nika-remote")


def list_goldens() -> list[dict[str, Any]]:
    require_incus()
    rows = []
    for inst in list_instances():
        if not inst["name"].startswith(GOLDEN_PREFIX):
            continue
        cfg = inst.get("config", {})
        variant = json.loads(cfg.get("user.nika.variant") or "{}")
        rows.append(
            {
                "name": inst["name"],
                "ready": cfg.get("user.nika.ready") == "true",
                "building": cfg.get("user.nika.ready") != "true" and _build_alive(cfg),
                # Current = what a trial would use now: same code, lab settings,
                # base and VM size, i.e. the key recomputed today matches its name.
                "current": bool(variant)
                and inst["name"] == f"{GOLDEN_PREFIX}{golden_key(variant)}",
                "variant": variant,
                "created_at": inst.get("created_at"),
            }
        )
    return rows


def prune_lock_files() -> int:
    """Delete build locks of golden VMs that no longer exist; return how many."""
    if not LOCK_DIR.is_dir():
        return 0
    existing = {row["name"].removeprefix(GOLDEN_PREFIX) for row in list_goldens()}
    removed = 0
    for lock in LOCK_DIR.glob("*.lock"):
        if lock.stem in existing:
            continue
        try:
            fd = os.open(lock, os.O_RDONLY)
        except OSError:
            continue  # gone, or another user's
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Waiters notice the unlink and lock a fresh file (``_key_lock``).
            lock.unlink()
            removed += 1
        except OSError:
            pass  # a build holds it, or another user owns it
        finally:
            os.close(fd)
    return removed


def _build_alive(cfg: dict[str, Any]) -> bool:
    """True when the process building this golden VM is still running."""
    if cfg.get("user.nika.host") not in (None, socket.gethostname()):
        return True  # another host's build: assume it is alive
    pid = int(cfg.get("user.nika.builder_pid") or 0)
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def delete_golden(name: str) -> None:
    if not name.startswith(GOLDEN_PREFIX):
        raise ValueError(f"{name} is not a golden VM")
    incus("delete", "--force", name)
