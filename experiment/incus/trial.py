"""One benchmark trial on a disposable copy of a golden VM.

The copy lives in its own Incus project (a stateful copy must keep the instance
name) on its own host-less bridge (it keeps the golden guest's MAC and IP). The
host reaches it only over vsock: local forwarders for the NIKA remote daemon and
the MCP gateway, which ``remote_override`` routes this process's lab operations to.
"""

from __future__ import annotations

import hashlib
import os
import socket
import time
from typing import Any

from nika.remote.config import RemoteConfig, remote_override
from nika.firecracker.trial import gateway_route
from experiment.incus.config import snapshot_settings
from experiment.incus.golden import (
    DAEMON_PORT,
    GATEWAY_PORT,
    GOLDEN_SNAPSHOT,
    Golden,
    ensure_golden,
)
from experiment.incus.incus import (
    IncusError,
    config_get,
    incus,
    list_projects,
    require_incus,
)
from experiment.incus.vsock import VsockForwarder, wait_http_ok

TRIAL_PROJECT_PREFIX = "nika-exp-t-"
_active: TrialLab | None = None


def trial_resource_names(trial_key: str) -> tuple[str, str]:
    """Incus project and bridge names for a trial (stable across retries)."""
    digest = hashlib.sha256(trial_key.encode()).hexdigest()[:10]
    return f"{TRIAL_PROJECT_PREFIX}{digest}", f"nkx{digest}"


class TrialLab:
    """Context manager: restore a golden copy and route lab operations to it."""

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
        self.project, self.network = trial_resource_names(trial_key)
        self.golden: Golden | None = None
        self.timings: dict[str, float] = {}
        self._forwarders: list[VsockForwarder] = []
        self._override = None

    def __enter__(self) -> TrialLab:
        global _active
        require_incus()
        started = time.monotonic()
        self.golden = ensure_golden(self.params)
        self.timings["golden_sec"] = round(time.monotonic() - started, 2)
        release_trial(self.trial_key)  # leftovers of an earlier attempt
        try:
            self._restore()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        _active = self
        return self

    def _restore(self) -> None:
        assert self.golden is not None
        vm = self.golden.name
        started = time.monotonic()
        # The project carries the owner so orphans can be found even if the
        # copy itself was never created.
        incus(
            "project",
            "create",
            self.project,
            *_project_options(
                owner=self.owner, trial=self.trial_key, golden=vm, pid=os.getpid()
            ),
        )
        incus(
            "network", "create", self.network, "ipv4.address=none", "ipv6.address=none"
        )
        incus(
            "copy",
            f"{vm}/{GOLDEN_SNAPSHOT}",
            vm,
            "--target-project",
            self.project,
            *("-d", "eth0,type=nic", "-d", f"eth0,network={self.network}"),
            *("-d", "eth0,name=eth0"),
        )
        # A copy drops boot_state; without it QEMU builds another device layout
        # and the saved RAM state does not load.
        boot_state = config_get(vm, "volatile.vm.boot_state")
        incus(
            "config",
            "set",
            vm,
            f"volatile.vm.boot_state={boot_state}",
            project=self.project,
        )
        incus("start", vm, project=self.project)
        self.timings["restore_sec"] = round(time.monotonic() - started, 2)
        cid = int(config_get(vm, "volatile.vsock_id", project=self.project))
        wait_http_ok(cid, DAEMON_PORT)
        self.timings["ready_sec"] = round(time.monotonic() - started, 2)
        bind_host, agent_host = gateway_route(self.sandbox_backend)
        daemon = VsockForwarder(cid, DAEMON_PORT)
        self._forwarders.append(daemon)
        gateway = VsockForwarder(cid, GATEWAY_PORT, host=bind_host)
        self._forwarders.append(gateway)
        config = RemoteConfig(
            enabled=True,
            url=f"http://127.0.0.1:{daemon.port}",
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

        assert self.golden is not None
        started = time.monotonic()
        new_id = remote_rebind_session(
            self.golden.session_id,
            new_session_id=session_id,
            result_dir=result_dir,
            session_dir=session_dir,
            restore_runtime=True,
        )
        if snapshot_settings().verify_after_restore:
            try:
                _client().verify_session(new_id, initial_baseline=True)
            except BaseException:
                # As a failed cold start does: leave no running session behind.
                close_session(session_id=new_id, undeploy=False, status="error")
                raise
        self.timings["attach_sec"] = round(time.monotonic() - started, 2)
        _record_trial_timings(new_id, self.golden, self.timings)
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
        if not release_trial(self.trial_key):
            raise IncusError(f"Trial resources remain: {self.project} / {self.network}")


def active_trial_lab() -> TrialLab | None:
    """The trial lab this process is running in, if any."""
    return _active


def _record_trial_timings(session_id: str, golden: Golden, timings: dict) -> None:
    from nika.utils.logger import log_event
    from nika.utils.session_store import SessionStore

    SessionStore().update_session(
        session_id, {"snapshot": {"golden": golden.name, **timings}}
    )
    log_event(
        "snapshot_restore",
        f"Restored {golden.name} for session {session_id}",
        session_id=session_id,
        golden=golden.name,
        **timings,
    )


def _project_options(*, owner: str, trial: str, golden: str, pid: int) -> list[str]:
    options = {
        "features.profiles": "false",
        "features.images": "false",
        "features.storage.volumes": "false",
        "features.networks": "false",
        "user.nika.owner": owner,
        "user.nika.trial": trial,
        # Keeps `nika snapshot clean` from deleting a golden VM in use.
        "user.nika.golden": golden,
        "user.nika.pid": str(pid),
        "user.nika.host": socket.gethostname(),
    }
    return [arg for key, value in options.items() for arg in ("-c", f"{key}={value}")]


def release_trial(trial_key: str, *, timeout: float = 120.0) -> bool:
    """Delete a trial's VM copy, bridge and project; True once they are gone.

    Retries while Incus is still busy with the copy, e.g. a start that kept
    running on the server after an interrupted client.
    """
    project, network = trial_resource_names(trial_key)
    deadline = time.monotonic() + timeout
    while True:
        for name in incus(
            "list", "--format", "csv", "-c", "n", project=project, check=False
        ).split():
            incus("delete", "--force", name, project=project, check=False)
        incus("network", "delete", network, check=False)
        incus("project", "delete", project, check=False)
        try:
            project_gone = project not in {p["name"] for p in list_projects()}
        except IncusError:
            project_gone = False  # Incus busy; check again
        network_gone = not incus(
            "network", "list", f"^{network}$", "--format", "csv", check=False
        ).strip()
        if project_gone and network_gone:
            return True
        if time.monotonic() > deadline:
            print(f"WARNING: [snapshot] could not release {project} / {network}")
            return False
        time.sleep(2)
