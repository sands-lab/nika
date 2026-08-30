"""Local workflow implementations that forward lab ops to a remote daemon."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from nika.config import resolve_results_root
from nika.remote.client import RemoteClient
from nika.remote.config import load_remote_config
from nika.remote.protocol import EnvStartRequest, FailureInjectRequest, PolicyMode
from nika.service.mcp_gateway.lifecycle import (
    ENV_GATEWAY_AGENT_URL,
    ENV_GATEWAY_URL,
)
from nika.utils.logger import bind_session_dir, log_event
from nika.utils.session import Session
from nika.utils.session_id import make_session_id
from nika.utils.session_store import SessionStore


def _rewrite_local_session_dir(session_id: str, local_dir: str) -> None:
    """Ensure store + run.json keep the local session_dir after artifact pulls."""
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
        if data.get("session_dir") != local_dir or not data.get("remote"):
            data["session_dir"] = local_dir
            data["remote"] = True
            run_path.write_text(
                json.dumps(data, indent=2, default=str), encoding="utf-8"
            )


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
    for key in ("agent_type", "model", "llm_provider", "reasoning_effort"):
        if key in local and key not in merged:
            merged[key] = local[key]
    # SessionStore.update_session skips failure_injections; write the full doc.
    path = store._path(session_id)
    merged["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
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
    backend: str | None = None,
    device_profile: str | None = None,
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
            backend=backend,
            device_profile=device_profile,
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
    resp = client.failure_inject(
        FailureInjectRequest(
            session_id=session.session_id,
            problem_names=list(problem_names),
            param_overrides=dict(param_overrides or {}),
        )
    )
    _sync_local_meta_from_remote(session.session_id, resp.session)
    _pull_and_fix_local(client, session.session_id, session.session_dir)
    bind_session_dir(session.session_dir)
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
    prior_url = os.environ.get(ENV_GATEWAY_URL)
    prior_agent = os.environ.get(ENV_GATEWAY_AGENT_URL)
    os.environ[ENV_GATEWAY_URL] = attach.gateway_base_url
    os.environ[ENV_GATEWAY_AGENT_URL] = attach.gateway_base_url
    try:
        yield attach.gateway_base_url, attach.gateway_port
    finally:
        try:
            client.mcp_detach(session_id)
        finally:
            if prior_url is None:
                os.environ.pop(ENV_GATEWAY_URL, None)
            else:
                os.environ[ENV_GATEWAY_URL] = prior_url
            if prior_agent is None:
                os.environ.pop(ENV_GATEWAY_AGENT_URL, None)
            else:
                os.environ[ENV_GATEWAY_AGENT_URL] = prior_agent


def remote_close_session(
    session_id: str | None = None,
    *,
    undeploy: bool = True,
    stop_all: bool = False,
) -> None:
    """Close remote lab session(s) and clear the local mirror."""
    client = _client()
    store = SessionStore()
    if stop_all:
        client.close_session(None, undeploy=undeploy, stop_all=True)
        for row in list(store.list_running_sessions()):
            sid = row["session_id"]
            try:
                store.delete_session(sid)
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
    # Mark local run finished similarly to Session.clear_session path.
    try:
        session.clear_session()
    except Exception:  # noqa: BLE001 - remote already closed
        try:
            store.delete_session(sid)
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
