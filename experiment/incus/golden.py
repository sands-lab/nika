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
from nika.firecracker.base import _repo_files
from nika.firecracker.image import (
    code_fingerprint,
    lab_settings,
    vm_cpus,
    vm_memory_mib,
)
from experiment.incus.config import snapshot_settings
from experiment.incus.incus import (
    config_get,
    guest,
    incus,
    instance_exists,
    require_incus,
    snapshot_names,
)
from experiment.incus.vsock import VsockForwarder, wait_http_ok

GOLDEN_PREFIX = "nika-exp-g-"
GOLDEN_SNAPSHOT = "golden"
DAEMON_PORT = 8700
GATEWAY_PORT = 8800
GUEST_REPO = "/home/ubuntu/nika"
# Host-wide: golden VMs belong to the Incus host, so every checkout must share
# the build locks.
LOCK_DIR = Path(tempfile.gettempdir()) / "nika-snapshot-locks"


@dataclass(frozen=True)
class Golden:
    """A ready golden VM."""

    name: str
    session_id: str


def golden_key(params: dict[str, Any]) -> str:
    base = snapshot_settings().base
    instance, snapshot = base.split("/", 1)
    payload = {
        "variant": params,
        "lab_settings": lab_settings(),
        "code": code_fingerprint(),
        "base": base,
        "base_snapshot": json.loads(
            incus("query", f"/1.0/instances/{instance}/snapshots/{snapshot}")
        ),
        "cpus": vm_cpus(params),
        "memory_mib": vm_memory_mib(params),
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
    memory = f"{vm_memory_mib(params) // 1024}GiB"
    state = f"{int(memory[:-3]) + 2}GiB"
    config = {
        "limits.cpu": vm_cpus(params),
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
        name,
        f"su - ubuntu -c 'cd {GUEST_REPO} && "
        "UV_CACHE_DIR=.nika_cache/uv/cache .venv/bin/uv sync --offline -q'",
        timeout=1800,
    )
    # A stateful snapshot keeps the page cache, but later stateless copies of
    # this disk must see the new files.
    guest(name, "sync && systemctl restart nika-remote")
