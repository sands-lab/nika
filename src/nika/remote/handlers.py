"""Server-side wrappers around existing NIKA workflows for the remote daemon."""

from __future__ import annotations

import logging
import os
from contextlib import ExitStack
from typing import Any

from nika.remote.artifacts import pack_session_dir
from nika.remote.protocol import (
    EnvStartRequest,
    EnvStartResponse,
    FailureInjectRequest,
    FailureInjectResponse,
    McpAttachRequest,
    McpAttachResponse,
    PolicyMode,
    SessionContainersResponse,
)
from nika.service.mcp_gateway.lifecycle import (
    McpGatewayManager,
    SANDBOX_GATEWAY_BIND_HOST,
    mcp_gateway_for_session,
)
from nika.utils.session import Session
from nika.utils.session_store import SessionStore
from nika.workflows.env.start import start_net_env
from nika.workflows.failure.inject import inject_failure
from nika.workflows.session.close import close_session
from nika.workflows.session.containers import list_session_containers
from nika.workflows.session.list import list_sessions

logger = logging.getLogger("nika.remote")


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
            )
        )
        self.gateways[session_id] = GatewayLease(stack=stack, manager=manager)
        # Prefer the agent-facing URL env set by lifecycle when sandbox=True.
        base = os.environ.get("NIKA_MCP_GATEWAY_AGENT_URL") or manager.base_url
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
        meta = SessionStore().get_session(session_id)
        session_dir = meta.get("session_dir")
        if not session_dir:
            # Fall back to Session helper if store row is incomplete.
            session = Session().load_running_session(session_id=session_id)
            session_dir = session.session_dir
        payload = pack_session_dir(str(session_dir))
    except Exception:
        logger.exception("artifacts pack failed session_id=%s", session_id)
        raise
    logger.info("artifacts pack done session_id=%s bytes=%s", session_id, len(payload))
    return payload
