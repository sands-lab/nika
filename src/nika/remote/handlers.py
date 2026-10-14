"""Server-side wrappers around existing NIKA workflows for the remote daemon."""

from __future__ import annotations

import logging
import os
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from nika.remote.artifacts import pack_session_dir
from nika.remote.protocol import (
    FOLLOWED_LOGS,
    EnvStartRequest,
    EnvStartResponse,
    FailureInjectRequest,
    FailureInjectResponse,
    McpAttachRequest,
    McpAttachResponse,
    PolicyMode,
    SessionContainersResponse,
    SessionLogResponse,
    SessionRebindRequest,
    SessionRebindResponse,
)
from nika.mcp.gateway.phase import phase_advance_token
from nika.mcp.gateway.session_registry import get_session
from nika.mcp.gateway.lifecycle import (
    McpGatewayManager,
    SANDBOX_GATEWAY_BIND_HOST,
    mcp_gateway_for_session,
)
from nika.runtime.factory import resolve_backend
from nika.utils.agent_session_id import make_agent_session_id
from nika.utils.logger import bind_session_dir, log_event
from nika.utils.session import Session
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session, load_session_meta_for_close
from nika.workflows.session.containers import list_session_containers
from nika.workflows.session.list import list_sessions
from nika.validation.presence import recheck_bound_artifact

logger = logging.getLogger("nika.remote")
# Cap one log read so a large backlog streams in several polls.
MAX_LOG_CHUNK = 4 << 20


class GatewayLease:
    """Holds an open ``mcp_gateway_for_session`` context for one session."""

    def __init__(self, stack: ExitStack, manager: McpGatewayManager) -> None:
        self.stack = stack
        self.manager = manager


class RemoteHandlerState:
    """Process-local state for attached MCP gateways."""

    def __init__(self) -> None:
        self.gateways: dict[str, GatewayLease] = {}

    def attach_gateway(
        self,
        session_id: str,
        *,
        scenario_name: str,
        policy_mode: PolicyMode,
        public_host: str,
        backend: str | None = None,
    ) -> McpAttachResponse:
        self.detach_gateway(session_id)
        stack = ExitStack()
        manager = stack.enter_context(
            mcp_gateway_for_session(
                session_id,
                scenario_name=scenario_name,
                policy_mode=policy_mode,
                host=SANDBOX_GATEWAY_BIND_HOST,
                sandbox=True,
                sandbox_agent_host=public_host,
                # Multi-backend scenarios (e.g. ISP on Containerlab) serve
                # backend-specific MCP servers; match the deployed lab.
                backend=backend,
            )
        )
        self.gateways[session_id] = GatewayLease(stack=stack, manager=manager)
        # Agent-facing URL for this session's gateway. Read from the manager,
        # never from process-global env: concurrent attaches run in parallel
        # threads.
        base = manager.agent_url or manager.base_url
        logger.info(
            "mcp attach done session_id=%s port=%s url=%s policy_mode=%s",
            session_id,
            manager.port,
            base,
            policy_mode,
        )
        return McpAttachResponse(
            session_id=session_id,
            gateway_port=manager.port,
            gateway_base_url=base,
            phase_token=phase_advance_token(session_id),
            session_token=get_session(session_id).access_token,
        )

    def detach_gateway(self, session_id: str) -> None:
        lease = self.gateways.pop(session_id, None)
        if lease is not None:
            lease.stack.close()
            logger.info("mcp detach done session_id=%s", session_id)
        else:
            logger.info("mcp detach noop session_id=%s (no lease)", session_id)


_STATE = RemoteHandlerState()


def get_handler_state() -> RemoteHandlerState:
    return _STATE


def session_public_dict(session_id: str) -> dict[str, Any]:
    meta = SessionStore().get_session(session_id)
    # Drop bulky nested blobs for list/get responses when enormous; keep full for now.
    return dict(meta)


def handle_env_start(request: EnvStartRequest) -> EnvStartResponse:
    logger.info(
        "env start begin scenario=%s session_id=%s topo_size=%s redeploy=%s",
        request.scenario,
        request.session_id or "(auto)",
        request.topo_size,
        request.redeploy,
    )
    try:
        session_id = start_net_env(
            request.scenario,
            request.topo_size,
            redeploy=request.redeploy,
            instance_tag=request.instance_tag,
            session_tag=request.session_tag,
            result_dir=request.result_dir,
            session_id=request.session_id,
            topo=request.topo,
            igp=request.igp,
            metric_strategy=request.metric_strategy,
            constant_metric=request.constant_metric,
            bgp_mode=request.bgp_mode,
            rpki=request.rpki,
            backend=request.backend,
            device_profile=request.device_profile,
            static_validation=request.static_validation,
        )
    except Exception:
        logger.exception("env start failed scenario=%s", request.scenario)
        raise
    logger.info(
        "env start done session_id=%s scenario=%s", session_id, request.scenario
    )
    return EnvStartResponse(
        session_id=session_id,
        session=session_public_dict(session_id),
    )


def handle_failure_inject(request: FailureInjectRequest) -> FailureInjectResponse:
    logger.info(
        "failure inject begin session_id=%s problems=%s overrides=%s",
        request.session_id,
        list(request.problem_names),
        dict(request.param_overrides or {}),
    )
    try:
        inject_failure(
            problem_names=list(request.problem_names),
            session_id=request.session_id,
            param_overrides=dict(request.param_overrides or {}),
        )
    except Exception:
        logger.exception(
            "failure inject failed session_id=%s problems=%s",
            request.session_id,
            list(request.problem_names),
        )
        raise
    logger.info(
        "failure inject done session_id=%s problems=%s",
        request.session_id,
        list(request.problem_names),
    )
    return FailureInjectResponse(
        session_id=request.session_id,
        session=session_public_dict(request.session_id),
    )


def handle_fault_artifact(session_id: str) -> dict[str, Any]:
    """Recheck the artifact on the instance that injected it in this daemon."""
    meta = SessionStore().get_session(session_id)
    if meta.get("status") != "running":
        raise ValueError(f"Session '{session_id}' is not running.")
    return recheck_bound_artifact(session_id)


def handle_mcp_attach(session_id: str, request: McpAttachRequest) -> McpAttachResponse:
    meta = SessionStore().get_session(session_id)
    if meta.get("status") != "running":
        raise ValueError(f"Session '{session_id}' is not running.")
    public_host = (request.public_host or "").strip() or "127.0.0.1"
    logger.info(
        "mcp attach begin session_id=%s scenario=%s policy_mode=%s public_host=%s",
        session_id,
        meta.get("scenario_name"),
        request.policy_mode,
        public_host,
    )
    try:
        return get_handler_state().attach_gateway(
            session_id,
            scenario_name=str(meta.get("scenario_name") or ""),
            backend=resolve_backend(meta),
            policy_mode=request.policy_mode,
            public_host=public_host,
        )
    except Exception:
        logger.exception("mcp attach failed session_id=%s", session_id)
        raise


def handle_mcp_detach(session_id: str) -> None:
    logger.info("mcp detach begin session_id=%s", session_id)
    get_handler_state().detach_gateway(session_id)


def handle_close_session(
    session_id: str | None,
    *,
    undeploy: bool = True,
    stop_all: bool = False,
) -> None:
    if stop_all:
        logger.info("session wipe begin undeploy=%s", undeploy)
        try:
            for sid in list(get_handler_state().gateways):
                get_handler_state().detach_gateway(sid)
            close_session(session_id=None, undeploy=undeploy, stop_all=True)
        except Exception:
            logger.exception("session wipe failed")
            raise
        logger.info("session wipe done")
        return
    if not session_id:
        raise ValueError("session_id is required unless stop_all=True")
    logger.info("session close begin session_id=%s undeploy=%s", session_id, undeploy)
    try:
        get_handler_state().detach_gateway(session_id)
        close_session(session_id=session_id, undeploy=undeploy, stop_all=False)
    except Exception:
        logger.exception("session close failed session_id=%s", session_id)
        raise
    logger.info("session close done session_id=%s", session_id)


def handle_list_sessions(*, running_only: bool = True) -> list[dict[str, Any]]:
    sessions = list_sessions(running_only=running_only)
    logger.info("session list running_only=%s count=%s", running_only, len(sessions))
    return sessions


def handle_get_session(session_id: str) -> dict[str, Any]:
    logger.info("session get session_id=%s", session_id)
    return session_public_dict(session_id)


def handle_session_containers(session_id: str) -> SessionContainersResponse:
    logger.info("session containers begin session_id=%s", session_id)
    try:
        resolved_id, lab_name, containers = list_session_containers(session_id)
    except Exception:
        logger.exception("session containers failed session_id=%s", session_id)
        raise
    logger.info(
        "session containers done session_id=%s lab=%s count=%s",
        resolved_id,
        lab_name,
        len(containers),
    )
    return SessionContainersResponse(
        session_id=resolved_id,
        lab_name=lab_name,
        containers=containers,
    )


def handle_artifacts(session_id: str) -> bytes:
    logger.info("artifacts pack begin session_id=%s", session_id)
    try:
        payload = pack_session_dir(_session_dir(session_id))
    except Exception:
        logger.exception("artifacts pack failed session_id=%s", session_id)
        raise
    logger.info("artifacts pack done session_id=%s bytes=%s", session_id, len(payload))
    return payload


def _session_dir(session_id: str) -> str:
    """Results directory of a running or closed session on this host.

    Closed sessions stay readable so the agent host can pull the events that
    closing writes (``env_stop``, ``session_cleared``).
    """
    session_dir = load_session_meta_for_close(session_id).get("session_dir")
    if not session_dir:
        raise FileNotFoundError(f"Session '{session_id}' has no results directory.")
    return str(session_dir)


def handle_session_log(session_id: str, name: str, offset: int) -> SessionLogResponse:
    """Return complete lines of a followed session log from byte ``offset``."""
    if name not in FOLLOWED_LOGS:
        raise ValueError(
            f"Log {name!r} cannot be followed; use one of {FOLLOWED_LOGS}."
        )
    path = Path(_session_dir(session_id)) / name
    server_time = time.time()
    if not path.is_file():
        return SessionLogResponse(data="", offset=0, server_time=server_time)
    with path.open("rb") as fh:
        size = fh.seek(0, os.SEEK_END)
        # A shorter file was replaced (e.g. by a rebind): read it from the start.
        start = offset if 0 <= offset <= size else 0
        fh.seek(start)
        chunk = fh.read(MAX_LOG_CHUNK)
        if b"\n" not in chunk:
            chunk += fh.readline()  # a single line longer than one chunk
    end = chunk.rfind(b"\n") + 1  # never hand out a partially written line
    return SessionLogResponse(
        data=chunk[:end].decode("utf-8"), offset=start + end, server_time=server_time
    )


def handle_session_rebind(
    session_id: str, request: SessionRebindRequest
) -> SessionRebindResponse:
    """Move a running session to a new id, agent id and results directory.

    A lab restored from a VM snapshot still carries the session it was built
    with. Each trial needs its own identity, and the build-time events in the
    session logs are kept as ``snapshot.<log>`` instead of opening the trial log.
    """
    new_id = request.session_id
    if not new_id or Path(new_id).name != new_id or new_id in (".", ".."):
        raise ValueError(f"Invalid session id {new_id!r}.")
    store = SessionStore()
    meta = store.get_session(session_id)
    if meta.get("status") != "running":
        raise ValueError(f"Session '{session_id}' is not running.")
    if session_id in get_handler_state().gateways:
        raise ValueError(f"Session '{session_id}' has an attached MCP gateway.")
    try:
        store.get_session(new_id)
    except FileNotFoundError:
        pass
    else:
        raise ValueError(f"Session '{new_id}' already exists.")
    old_dir = Path(str(meta["session_dir"]))
    new_dir = old_dir.parent / new_id
    if new_dir.exists():
        raise ValueError(f"Results directory {new_dir} already exists.")
    old_dir.rename(new_dir)
    for name in FOLLOWED_LOGS:
        log = new_dir / name
        if log.is_file():
            log.rename(new_dir / f"snapshot.{name}")
    now = datetime.now(timezone.utc).isoformat()
    doc = dict(meta)
    doc.update(
        session_id=new_id,
        agent_session_id=request.agent_session_id or make_agent_session_id(),
        session_dir=str(new_dir),
        created_at=now,
        updated_at=now,
        rebound_from=session_id,
    )
    store.create_session(doc)
    store.delete_session(session_id, status="finished")
    session = Session()
    session.load_running_session(session_id=new_id)
    session._write_run_json({k: v for k, v in session.__dict__.items() if k != "store"})
    bind_session_dir(new_dir)
    log_event(
        "session_rebind",
        f"Session {session_id} continues as {new_id}",
        session_id=new_id,
        rebound_from=session_id,
        lab_name=meta.get("lab_name"),
    )
    logger.info("session rebind %s -> %s", session_id, new_id)
    return SessionRebindResponse(session_id=new_id, session=session_public_dict(new_id))


def handle_session_verify(session_id: str) -> dict[str, Any]:
    """Run the startup lab verification of a running session again.

    Uses the same check and retry budget as ``nika env run`` (``verify_lab_with_retry``),
    e.g. to confirm a lab restored from a snapshot before injecting a fault.
    """
    from nika.net_env.verify import verify_lab_with_retry
    from nika.workflows.session.close import session_net_env

    meta = SessionStore().get_session(session_id)
    if meta.get("status") != "running":
        raise ValueError(f"Session '{session_id}' is not running.")
    bind_session_dir(str(meta["session_dir"]))
    net_env = session_net_env(meta)
    started = time.monotonic()
    result = verify_lab_with_retry(net_env)
    verified = result is not None  # None: the scenario has no startup verification
    outcome = "passed again" if verified else "skipped (no startup verification)"
    log_event(
        "env_reverify",
        f"Lab verification {outcome} for {meta.get('scenario_name')} "
        f"({meta.get('lab_name')})",
        session_id=session_id,
        verified=verified,
        checks=(result or {}).get("checks"),
        duration_ms=round((time.monotonic() - started) * 1000, 1),
    )
    return {
        "session_id": session_id,
        "verified": verified,
        "checks": (result or {}).get("checks"),
    }
