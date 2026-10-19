"""One benchmark trial on a disposable copy of a lab image.

The copy runs in ``<root>/trials/<digest>/`` with a copy-on-write clone of the
lab image disk and, through the relative paths the image was built with, its own
vsock socket. Its memory comes from the lab image's memory file: mapped privately
(pages load on first access), or, with huge pages, served by the
``nika.firecracker.uffd`` page server, which also prefetches all of it. The host
reaches the copy only through vsock forwarders for the NIKA remote daemon and
the MCP gateway, which ``remote_override`` routes this process's lab
operations to.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from nika.remote.config import RemoteConfig, remote_override
from nika.firecracker.config import firecracker_settings
from nika.firecracker.image import (
    DAEMON_PORT,
    GATEWAY_PORT,
    MEMORY,
    STATE,
    LabImage,
    _clone,
    ensure_image,
    root,
)
from nika.firecracker.vmm import (
    DISK,
    VSOCK,
    Firecracker,
    VmmError,
    VsockForwarder,
    firecracker_binary,
    kill_pid_file,
    pid_alive,
    wait_http_ok,
)

TRIAL_META = "trial.json"
_active: TrialLab | None = None


def trials_dir() -> Path:
    return root() / "trials"


def trial_dir(trial_key: str) -> Path:
    """Run directory of a trial (stable across retries)."""
    return trials_dir() / hashlib.sha256(trial_key.encode()).hexdigest()[:16]


class TrialLab:
    """Context manager: restore a lab image copy and route lab operations to it."""

    def __init__(
        self,
        params: dict[str, Any],
        *,
        trial_key: str,
        owner: str,  # results root of the run, for finding its orphaned copies
        sandbox_backend: str | None = None,
    ) -> None:
        self.params = params
        self.trial_key = trial_key
        self.owner = owner
        # Sandbox runtime of the trial's agent ("sbx", "e2b"), None when unsandboxed.
        self.sandbox_backend = sandbox_backend
        self.dir = trial_dir(trial_key)
        self.image: LabImage | None = None
        self.timings: dict[str, float] = {}
        self._forwarders: list[VsockForwarder] = []
        self._override = None

    def __enter__(self) -> TrialLab:
        global _active
        started = time.monotonic()
        self.image = ensure_image(self.params)
        self.timings["image_sec"] = round(time.monotonic() - started, 2)
        release_trial(self.trial_key)  # leftovers of an earlier attempt
        try:
            self._restore()
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise
        _active = self
        return self

    def _restore(self) -> None:
        assert self.image is not None
        settings = firecracker_settings()
        started = time.monotonic()
        self.dir.mkdir(parents=True)
        (self.dir / TRIAL_META).write_text(
            json.dumps(
                {
                    "trial": self.trial_key,
                    "owner": self.owner,
                    "image": self.image.key,
                    "pid": os.getpid(),
                    "host": socket.gethostname(),
                }
            )
        )
        _clone(self.image.path / DISK, self.dir / DISK)
        vm = Firecracker(self.dir, firecracker_binary(settings.binary))
        vm.start()
        uffd = None
        if settings.huge_pages:
            uffd = self.dir / "uffd.sock"
            _start_page_server(self.dir, uffd, self.image.path / MEMORY)
        vm.load(
            state=self.image.path / STATE,
            memory=self.image.path / MEMORY,
            uffd_socket=uffd,
        )
        self.timings["restore_sec"] = round(time.monotonic() - started, 2)
        uds = self.dir / VSOCK
        wait_http_ok(uds, DAEMON_PORT)
        self.timings["ready_sec"] = round(time.monotonic() - started, 2)
        bind_host, agent_host = gateway_route(self.sandbox_backend)
        self._forwarders.append(VsockForwarder(uds, DAEMON_PORT))
        gateway = VsockForwarder(uds, GATEWAY_PORT, host=bind_host)
        self._forwarders.append(gateway)
        config = RemoteConfig(
            enabled=True,
            url=f"http://127.0.0.1:{self._forwarders[0].port}",
            artifact_root="",
            gateway_ports={GATEWAY_PORT: gateway.port},
            gateway_host=agent_host,
            disposable_lab=True,
        )
        self._override = remote_override(config)
        self._override.__enter__()

    def attach(
        self,
        *,
        session_id: str,
        result_dir: str | None = None,
        session_dir: str | None = None,
    ) -> str:
        """Give the restored lab session the trial's identity; return its id."""
        from nika.remote.workflows import _client, remote_rebind_session
        from nika.workflows.session.close import close_session

        assert self.image is not None
        started = time.monotonic()
        new_id = remote_rebind_session(
            self.image.session_id,
            new_session_id=session_id,
            result_dir=result_dir,
            session_dir=session_dir,
        )
        if firecracker_settings().verify_after_restore:
            try:
                _client().verify_session(new_id)
            except BaseException:
                # As a failed cold start does: leave no running session behind.
                close_session(session_id=new_id, undeploy=False, status="error")
                raise
        self.timings["attach_sec"] = round(time.monotonic() - started, 2)
        _record_trial_timings(new_id, self.image, self.timings)
        return new_id

    def __exit__(self, *exc_info: object) -> None:
        global _active
        _active = None
        if self._override is not None:
            self._override.__exit__(None, None, None)
            self._override = None
        for forwarder in self._forwarders:
            forwarder.close()
        self._forwarders = []
        if exc_info[0] is not None:
            _print_console_tail(trial_dir(self.trial_key), self.trial_key)
        release_trial(self.trial_key)


def _start_page_server(run_dir: Path, sock: Path, memory: Path) -> None:
    log = open(run_dir / "uffd.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "nika.firecracker.uffd", str(sock), str(memory)],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    log.close()
    (run_dir / "uffd.pid").write_text(str(proc.pid))
    deadline = time.monotonic() + 10
    while not sock.exists():
        if proc.poll() is not None or time.monotonic() > deadline:
            raise VmmError(f"page server did not start; see {run_dir}/uffd.log")
        time.sleep(0.01)


def active_trial_lab() -> TrialLab | None:
    """The trial lab this process is running in, if any."""
    return _active


def gateway_route(sandbox_backend: str | None) -> tuple[str, str | None]:
    """Where the gateway forwarder listens and the host the agent dials.

    Each sandbox runtime declares the host its agents use for the gateway: a
    Docker Sandbox reaches this host through a bridge name, an E2B tunnel dials
    from this host. Unsandboxed agents run on this host.
    """
    if sandbox_backend is None:
        return "127.0.0.1", None
    from agent.sandbox.backend import backend_class

    host = backend_class(sandbox_backend).gateway_agent_host
    if host in ("127.0.0.1", "localhost"):
        return "127.0.0.1", None
    return "0.0.0.0", host


def _record_trial_timings(session_id: str, image: LabImage, timings: dict) -> None:
    from nika.utils.logger import log_event
    from nika.utils.session_store import SessionStore

    SessionStore().update_session(
        session_id, {"firecracker": {"image": image.key, **timings}}
    )
    log_event(
        "lab_image_restore",
        f"Restored lab image {image.key} for session {session_id}",
        session_id=session_id,
        image=image.key,
        **timings,
    )


def _print_console_tail(path: Path, trial_key: str, lines: int = 40) -> None:
    """Show a failed copy's guest console before its directory is deleted."""
    try:
        text = (path / "console.log").read_text(errors="replace")
    except OSError:
        return
    tail = "\n".join(text.splitlines()[-lines:])
    print(f"[firecracker] console of failed copy {trial_key}:\n{tail}", flush=True)


def release_trial(trial_key: str) -> None:
    """Stop a trial's VM copy and page server and delete its run directory."""
    _release_dir(trial_dir(trial_key))


def _release_dir(path: Path) -> None:
    kill_pid_file(path / "firecracker.pid")
    kill_pid_file(path / "uffd.pid")
    shutil.rmtree(path, ignore_errors=True)


def trial_rows(owner: str | None = None) -> list[dict[str, Any]]:
    """Trial copies on this host, optionally of one owner, with liveness."""
    directory = trials_dir()
    if not directory.is_dir():
        return []
    host = socket.gethostname()
    rows = []
    for path in sorted(directory.iterdir()):
        try:
            meta = json.loads((path / TRIAL_META).read_text())
        except (OSError, ValueError):
            meta = {}
        if meta.get("host") not in (None, host):
            continue
        if owner is not None and meta.get("owner") != owner:
            continue
        pid = int(meta.get("pid") or 0)
        rows.append(
            {
                "path": path,
                "trial": meta.get("trial", ""),
                "owner": meta.get("owner", ""),
                "image": meta.get("image", ""),
                "alive": bool(pid) and pid_alive(pid),
            }
        )
    return rows


def reap_orphan_trials(owner: str | None = None) -> list[str]:
    """Release trial copies whose process is gone (of ``owner``, or all on this host)."""
    reaped = []
    for row in trial_rows(owner):
        if row["alive"]:
            continue
        _release_dir(row["path"])
        reaped.append(row["trial"] or row["path"].name)
    return reaped
