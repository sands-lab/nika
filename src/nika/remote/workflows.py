"""Local workflow implementations that forward lab ops to a remote daemon."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from uuid import uuid4

from agent.sandbox.config import ENV_GATEWAY_SESSION_TOKEN
from nika.config import resolve_results_root
from nika.remote.client import RemoteClient
from nika.remote.config import load_remote_config
from nika.remote.protocol import (
    FOLLOWED_LOGS,
    EnvStartRequest,
    FailureInjectRequest,
    PolicyMode,
)
from nika.mcp.gateway.lifecycle import (
    ENV_GATEWAY_AGENT_URL,
    ENV_GATEWAY_PHASE_TOKEN,
    ENV_GATEWAY_URL,
)
from nika.utils.logger import bind_session_dir, log_event
from nika.utils.agent_session_id import make_agent_session_id
from nika.utils.session import Session
from nika.utils.session_artifacts import write_json_atomic
from nika.utils.session_id import make_session_id
from nika.utils.session_store import SessionStore

logger = logging.getLogger("nika.remote")


# Session fields only the agent host knows; remote copies of the session must
# not drop them.
LOCAL_ONLY_KEYS = (
    "agent_type",
    "model",
    "llm_provider",
    "reasoning_effort",
    "max_tokens",
    # The agent's sandbox runs on this host; session close removes it.
    "sandbox_backend",
    "sandbox_url",
    "sandbox_domain",
    "remote_clock_offset_sec",
    "snapshot",
)


def _rewrite_local_session_dir(session_id: str, local_dir: str) -> None:
    """Ensure store + run.json keep local fields after artifact pulls."""
    store = SessionStore()
    try:
        meta = store.get_session(session_id)
    except FileNotFoundError:
        return
    if meta.get("session_dir") != local_dir:
        store.update_session(session_id, {"session_dir": local_dir, "remote": True})
    run_path = Path(local_dir) / "run.json"
    if run_path.is_file():
        try:
            data = json.loads(run_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        local = {key: meta[key] for key in LOCAL_ONLY_KEYS if key in meta}
        local.update(session_dir=local_dir, remote=True)
        if any(data.get(key) != value for key, value in local.items()):
            write_json_atomic(run_path, {**data, **local})


def _pull_and_fix_local(client: RemoteClient, session_id: str, local_dir: str) -> None:
    client.pull_artifacts(session_id, local_dir)
    _rewrite_local_session_dir(session_id, local_dir)


def pull_session_artifacts(
    session_id: str, local_dir: str, *, client: RemoteClient | None = None
) -> None:
    """Download remote session artifacts and keep the local session_dir path."""
    _pull_and_fix_local(client or _client(), session_id, local_dir)


def _client() -> RemoteClient:
    return RemoteClient(load_remote_config())


class RemoteLogFollower:
    """Copy new lines of the remote session logs into the local mirror as they appear.

    Artifact pulls only run between workflow steps; following keeps
    ``nika.jsonl`` and ``messages.jsonl`` current while a remote step runs.
    Lines already present locally (written here or pulled before) are skipped,
    so later artifact pulls and this follower never duplicate a line.
    """

    def __init__(
        self,
        client: RemoteClient,
        session_id: str,
        local_dir: str | Path,
        *,
        interval_sec: float = 1.0,
    ) -> None:
        self._client = client
        self._session_id = session_id
        self._dir = Path(local_dir)
        self._interval = interval_sec
        self._offsets = dict.fromkeys(FOLLOWED_LOGS, 0)
        self._seen: dict[str, set[str]] = {}
        for name in FOLLOWED_LOGS:
            path = self._dir / name
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            self._seen[name] = set(text.splitlines())
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        # Agent-host minus lab-host wall clock, from the first answered read.
        self.clock_offset_sec: float | None = None

    def poll(self) -> None:
        with self._lock:
            self._poll()

    def _poll(self) -> None:
        for name in FOLLOWED_LOGS:
            sent = time.time()
            chunk = self._client.read_log(self._session_id, name, self._offsets[name])
            if self.clock_offset_sec is None:
                midpoint = (sent + time.time()) / 2
                self.clock_offset_sec = round(midpoint - chunk.server_time, 3)
            self._offsets[name] = chunk.offset
            seen = self._seen[name]
            new = [
                line
                for line in chunk.data.splitlines()
                if line.strip() and line not in seen
            ]
            if not new:
                continue
            seen.update(new)
            with (self._dir / name).open("a", encoding="utf-8") as fh:
                fh.write("\n".join(new) + "\n")

    def _try_poll(self) -> None:
        # Best effort: the artifact pull after each step syncs the full logs.
        try:
            self.poll()
        except Exception:  # noqa: BLE001
            logger.debug("remote log follow failed", exc_info=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self._try_poll()

    def __enter__(self) -> "RemoteLogFollower":
        self._thread = threading.Thread(
            target=self._run, name=f"remote-logs-{self._session_id}", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30.0)
        if self._thread is None or not self._thread.is_alive():
            self._try_poll()
        if self.clock_offset_sec is not None:
            _record_clock_offset(self._session_id, self.clock_offset_sec)


def _record_clock_offset(session_id: str, offset_sec: float) -> None:
    """Store the agent-host minus lab-host clock offset on the local session."""
    store = SessionStore()
    try:
        previous = store.get_session(session_id).get("remote_clock_offset_sec")
    except FileNotFoundError:
        return
    store.update_session(session_id, {"remote_clock_offset_sec": offset_sec})
    if previous is None:
        log_event(
            "remote_clock_offset",
            f"Agent-host minus lab-host clock: {offset_sec:+.1f}s; "
            'events with "origin": "remote" use the lab-host clock.',
            session_id=session_id,
            offset_sec=offset_sec,
        )


def follow_remote_logs(session_id: str, local_dir: str | Path) -> RemoteLogFollower:
    """Context manager that streams remote session logs during a remote step."""
    return RemoteLogFollower(_client(), session_id, local_dir)


def _mirror_session_locally(
    remote_session: dict[str, Any],
    *,
    result_dir: str | None,
    session_dir: str | None,
) -> Session:
    """Create a local running-session mirror for a remote lab session."""
    session_id = str(remote_session["session_id"])
    if session_dir is not None:
        local_dir = str(Path(session_dir))
    else:
        local_dir = str(resolve_results_root(result_dir) / session_id)
    Path(local_dir).mkdir(parents=True, exist_ok=True)

    store = SessionStore()
    # Replace any stale local row for the same id.
    try:
        store.delete_session(session_id)
    except Exception:  # noqa: BLE001 - best effort
        pass

    payload = dict(remote_session)
    payload["session_id"] = session_id
    payload["session_dir"] = local_dir
    payload["status"] = "running"
    payload["remote"] = True
    payload["remote_session_dir"] = remote_session.get("session_dir")
    store.create_session(payload)

    session = Session()
    session.load_running_session(session_id=session_id)
    session._write_run_json({k: v for k, v in session.__dict__.items() if k != "store"})
    return session


def _sync_local_meta_from_remote(
    session_id: str, remote_session: dict[str, Any]
) -> None:
    store = SessionStore()
    local = store.get_session(session_id)
    local_dir = local.get("session_dir")
    remote_dir = remote_session.get("session_dir")
    merged = dict(remote_session)
    merged["session_dir"] = local_dir
    merged["remote"] = True
    if remote_dir:
        merged["remote_session_dir"] = remote_dir
    merged["status"] = local.get("status", "running")
    # Preserve local-only fields if remote omitted them.
    for key in LOCAL_ONLY_KEYS:
        if key in local and key not in merged:
            merged[key] = local[key]
    # SessionStore.update_session skips failure_injections; write the full doc.
    path = store._path(session_id)
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json_atomic(path, merged, ensure_ascii=False)
    store.index.upsert_from_doc(merged)
    session = Session()
    session.load_running_session(session_id=session_id)
    session._write_run_json({k: v for k, v in session.__dict__.items() if k != "store"})


def remote_start_net_env(
    scenario: str,
    topo_size: str | None,
    *,
    redeploy: bool = True,
    instance_tag: str | None = None,
    session_tag: str | None = None,
    result_dir: str | None = None,
    session_id: str | None = None,
    session_dir: str | None = None,
    topo: str | None = None,
    igp: str | None = None,
    metric_strategy: str | None = None,
    constant_metric: int | None = None,
    bgp_mode: str | None = None,
    rpki: bool | None = None,
    backend: str | None = None,
    device_profile: str | None = None,
    static_validation: bool = False,
) -> str:
    """Deploy a lab on the remote host and mirror the session locally."""
    suffix = uuid4().hex[:6]
    resolved_session_id = session_id or make_session_id(
        session_tag=session_tag, suffix=suffix
    )
    client = _client()
    resp = client.env_start(
        EnvStartRequest(
            scenario=scenario,
            topo_size=topo_size,
            redeploy=redeploy,
            instance_tag=instance_tag,
            session_tag=session_tag,
            session_id=resolved_session_id,
            result_dir=None,  # remote uses its own results root
            topo=topo,
            igp=igp,
            metric_strategy=metric_strategy,
            constant_metric=constant_metric,
            bgp_mode=bgp_mode,
            rpki=rpki,
            backend=backend,
            device_profile=device_profile,
            static_validation=static_validation,
        )
    )
    session = _mirror_session_locally(
        resp.session,
        result_dir=result_dir,
        session_dir=session_dir,
    )
    bind_session_dir(session.session_dir)
    _pull_and_fix_local(client, resp.session_id, session.session_dir)
    log_event(
        "remote_env_start",
        f"Remote lab started for {scenario} — session {resp.session_id}",
        session_id=resp.session_id,
        scenario=scenario,
        remote=True,
    )
    return resp.session_id


def remote_rebind_session(
    session_id: str,
    *,
    new_session_id: str | None = None,
    result_dir: str | None = None,
    session_dir: str | None = None,
) -> str:
    """Give a running remote session a fresh identity and mirror it locally.

    Use after restoring a lab-host snapshot: the restored session keeps the id,
    agent id and logs of the build it was taken from.
    """
    resolved_id = new_session_id or make_session_id()
    client = _client()
    resp = client.rebind_session(
        session_id, resolved_id, agent_session_id=make_agent_session_id()
    )
    session = _mirror_session_locally(
        resp.session, result_dir=result_dir, session_dir=session_dir
    )
    bind_session_dir(session.session_dir)
    _pull_and_fix_local(client, resp.session_id, session.session_dir)
    log_event(
        "remote_session_rebind",
        f"Remote session {session_id} continues as {resp.session_id}",
        session_id=resp.session_id,
        rebound_from=session_id,
        remote=True,
    )
    return resp.session_id


def remote_inject_failure(
    problem_names: list[str],
    *,
    session_id: str | None = None,
    param_overrides: dict[str, str] | None = None,
) -> None:
    """Inject faults on the remote host and sync artifacts locally."""
    session = Session()
    session.load_running_session(session_id=session_id)
    client = _client()
    bind_session_dir(session.session_dir)
    with RemoteLogFollower(client, session.session_id, session.session_dir):
        resp = client.failure_inject(
            FailureInjectRequest(
                session_id=session.session_id,
                problem_names=list(problem_names),
                param_overrides=dict(param_overrides or {}),
            )
        )
    _sync_local_meta_from_remote(session.session_id, resp.session)
    _pull_and_fix_local(client, session.session_id, session.session_dir)
    log_event(
        "remote_failure_inject",
        f"Remote failure injected for session {session.session_id}: {problem_names}",
        session_id=session.session_id,
        problems=problem_names,
        remote=True,
    )


@contextmanager
def remote_mcp_gateway(
    session_id: str,
    *,
    policy_mode: PolicyMode = "two_phase",
) -> Iterator[tuple[str, int]]:
    """Attach a remote MCP gateway; yield ``(gateway_base_url, port)``."""
    client = _client()
    cfg = load_remote_config()
    attach = client.mcp_attach(
        session_id,
        policy_mode=policy_mode,
        public_host=cfg.host,
    )
    exported = {
        ENV_GATEWAY_URL: attach.gateway_base_url,
        ENV_GATEWAY_AGENT_URL: attach.gateway_base_url,
        # Host orchestration advances the phase; sandbox env filters NIKA_*TOKEN.
        ENV_GATEWAY_PHASE_TOKEN: attach.phase_token,
        ENV_GATEWAY_SESSION_TOKEN: attach.session_token,
    }
    prior = {key: os.environ.get(key) for key in exported}
    os.environ.update(exported)
    # The port this host dials; it differs from the lab host's gateway port when
    # the client maps it (e.g. a local forwarder to a lab VM).
    port = urlparse(attach.gateway_base_url).port or attach.gateway_port
    try:
        yield attach.gateway_base_url, port
    finally:
        try:
            client.mcp_detach(session_id)
        finally:
            for key, value in prior.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


def remote_close_session(
    session_id: str | None = None,
    *,
    undeploy: bool = True,
    stop_all: bool = False,
    status: str = "finished",
) -> None:
    """Close remote lab session(s) and clear the local mirror."""
    client = _client()
    disposable = client.config.disposable_lab
    if disposable:
        undeploy = False  # the lab host is discarded after the trial
    store = SessionStore()
    if stop_all:
        client.close_session(None, undeploy=undeploy, stop_all=True)
        for row in list(store.list_running_sessions()):
            sid = row["session_id"]
            try:
                store.delete_session(sid, status=status)
            except Exception:  # noqa: BLE001
                pass
        return

    session = Session()
    session.load_running_session(session_id=session_id)
    sid = session.session_id
    # Pull final artifacts before remote teardown removes runtime docs.
    try:
        _pull_and_fix_local(client, sid, session.session_dir)
    except Exception:  # noqa: BLE001 - still attempt remote close
        pass
    client.close_session(sid, undeploy=undeploy, stop_all=False)
    if disposable:
        bind_session_dir(session.session_dir)
        log_event(
            "env_stop",
            f"Discarded the lab with its VM copy ({sid}); nothing to undeploy.",
            session_id=sid,
            discarded=True,
        )
    # Closing logs its own events (env_stop, session_cleared) on the lab host.
    try:
        _pull_and_fix_local(client, sid, session.session_dir)
    except Exception:  # noqa: BLE001 - the trial's results are already local
        pass
    # Mark local run with the same terminal status as Session.clear_session.
    try:
        session.clear_session(status=status)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 - remote already closed
        try:
            store.delete_session(sid, status=status)
        except Exception:  # noqa: BLE001
            pass


def remote_list_sessions(*, running_only: bool = True) -> list[dict[str, Any]]:
    """List sessions from the remote daemon, rewriting session_dir to local mirrors."""
    client = _client()
    remote_rows = client.list_sessions(running_only=running_only)
    store = SessionStore()
    out: list[dict[str, Any]] = []
    for row in remote_rows:
        sid = row.get("session_id")
        local = dict(row)
        local["remote"] = True
        if sid:
            try:
                mirrored = store.get_session(str(sid))
                if mirrored.get("session_dir"):
                    local["session_dir"] = mirrored["session_dir"]
            except FileNotFoundError:
                pass
        out.append(local)
    return out


def remote_list_session_containers(
    session_id: str | None = None,
    *,
    store: SessionStore | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """List lab containers on the remote host for a locally mirrored session."""
    from nika.utils.session_resolve import resolve_running_session_id

    session_store = store or SessionStore()
    resolved_id = resolve_running_session_id(session_id, store=session_store)
    resp = _client().session_containers(resolved_id)
    return resp.session_id, resp.lab_name, list(resp.containers)
