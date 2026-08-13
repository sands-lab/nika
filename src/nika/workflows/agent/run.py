"""Run a troubleshooting agent against the current session task."""

import asyncio
import logging
import os

from agent.registry import create_agent
from agent.sandbox import SANDBOX_SUPPORTED_AGENTS, SbxSandboxManager, sbx_available
from agent.sandbox.config import resolve_sandbox_config, sandbox_gateway_agent_host
from nika.service.mcp_gateway.lifecycle import (
    ENV_GATEWAY_AGENT_URL,
    mcp_gateway_for_session,
)
from nika.utils.agent_config import (
    resolve_agent_model,
    resolve_agent_type,
    resolve_llm_provider,
    resolve_max_steps,
    resolve_reasoning_effort,
)
from nika.utils.logger import bind_session_dir, log_error_event, log_event
from agent.utils.provider_env import provider_env_context
from nika.utils.session import Session

logging.basicConfig(level=logging.INFO)


def _gateway_policy_mode(agent_type: str) -> str:
    return "unified" if agent_type == "community.sade" else "two_phase"


def start_agent(
    agent_type: str | None = None,
    llm_provider: str | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    *,
    session_id: str | None = None,
    reasoning_effort: str | None = None,
    stream_output: bool = True,
    sandbox_keep_container: bool | None = None,
    sandbox_cpus: str | None = None,
    sandbox_memory: str | None = None,
    sandbox_offline_sdk_wheels: bool | None = None,
) -> None:
    """Load the running session, run the agent on ``task_description``, then end the session."""
    from nika.run_config.legacy import warn_legacy_operational_env
    from nika.utils.agent_config import apply_custom_provider_env

    warn_legacy_operational_env()
    apply_custom_provider_env()

    agent_type = resolve_agent_type(agent_type)
    max_steps = resolve_max_steps(max_steps)
    reasoning_effort = resolve_reasoning_effort(reasoning_effort)
    llm_provider = resolve_llm_provider(llm_provider, agent_type=agent_type)
    model = resolve_agent_model(agent_type, model, llm_provider=llm_provider)
    sandbox_config = resolve_sandbox_config(
        keep_container=sandbox_keep_container,
        cpus=sandbox_cpus,
        memory=sandbox_memory,
        offline_sdk_wheels=sandbox_offline_sdk_wheels,
    )
    use_sandbox = agent_type in SANDBOX_SUPPORTED_AGENTS

    session = Session()
    session.load_running_session(session_id=session_id)
    session.update_session("agent_type", agent_type)
    if llm_provider is not None:
        session.update_session("llm_provider", llm_provider)
    session.update_session("model", model)
    if reasoning_effort is not None:
        session.update_session("reasoning_effort", reasoning_effort)
    session.start_session()

    bind_session_dir(session.session_dir)
    log_event(
        "agent_start",
        f"Starting agent: {agent_type} (model={model}) in session {session.session_id}"
        + (" [sandbox]" if use_sandbox else ""),
        session_id=session.session_id,
        agent_type=agent_type,
        model=model,
        sandbox=use_sandbox,
    )
    if agent_type == "cli.codex" and stream_output:
        effort_line = (
            f" | Reasoning effort: {reasoning_effort}" if reasoning_effort else ""
        )
        mode_line = " | Sandbox: enabled"
        print(
            f"Session {session.session_id}\n"
            f"Agent: cli.codex | Model: {model}{effort_line}{mode_line}\n"
            f"Results: {session.session_dir}\n",
            flush=True,
        )
    try:
        from nika.remote.config import is_remote_enabled

        if is_remote_enabled():
            from nika.remote.workflows import pull_session_artifacts, remote_mcp_gateway

            with remote_mcp_gateway(
                session.session_id,
                policy_mode=_gateway_policy_mode(agent_type),  # type: ignore[arg-type]
            ) as (gateway_base_url, gateway_port):
                if use_sandbox:
                    if not sbx_available():
                        raise RuntimeError(
                            "Docker Sandboxes CLI (sbx) is not available. "
                            "Install docker-sbx and run `sbx login`."
                        )
                    SbxSandboxManager(sandbox_config).run(
                        session=session,
                        agent_type=agent_type,
                        model=model,
                        max_steps=max_steps,
                        reasoning_effort=reasoning_effort,
                        llm_provider=llm_provider,
                        mcp_gateway_agent_url=gateway_base_url,
                        gateway_port=gateway_port,
                        stream_output=stream_output,
                    )
                else:
                    with provider_env_context(
                        agent_type=agent_type,
                        provider=llm_provider or "openai",
                    ):
                        agent = create_agent(
                            agent_type,
                            session_id=session.session_id,
                            llm_provider=llm_provider,
                            model=model,
                            max_steps=max_steps,
                            reasoning_effort=reasoning_effort,
                            stream_output=stream_output,
                        )
                        asyncio.run(
                            agent.run(task_description=session.task_description)
                        )
            # Pull remote-written artifacts (e.g. submission.json) after the agent.
            pull_session_artifacts(session.session_id, session.session_dir)
        else:
            with mcp_gateway_for_session(
                session.session_id,
                scenario_name=session.scenario_name,
                policy_mode=_gateway_policy_mode(agent_type),  # type: ignore[arg-type]
                sandbox=use_sandbox,
                sandbox_agent_host=sandbox_gateway_agent_host(),
                backend=getattr(session, "backend", None),
            ) as gateway_manager:
                if use_sandbox:
                    if not sbx_available():
                        raise RuntimeError(
                            "Docker Sandboxes CLI (sbx) is not available. "
                            "Install docker-sbx and run `sbx login`."
                        )
                    gateway_agent_url = os.environ.get(ENV_GATEWAY_AGENT_URL, "")
                    if not gateway_agent_url:
                        raise RuntimeError(
                            f"{ENV_GATEWAY_AGENT_URL} was not set for sandbox execution"
                        )
                    SbxSandboxManager(sandbox_config).run(
                        session=session,
                        agent_type=agent_type,
                        model=model,
                        max_steps=max_steps,
                        reasoning_effort=reasoning_effort,
                        llm_provider=llm_provider,
                        mcp_gateway_agent_url=gateway_agent_url,
                        gateway_port=gateway_manager.port,
                        stream_output=stream_output,
                    )
                else:
                    with provider_env_context(
                        agent_type=agent_type,
                        provider=llm_provider or "openai",
                    ):
                        agent = create_agent(
                            agent_type,
                            session_id=session.session_id,
                            llm_provider=llm_provider,
                            model=model,
                            max_steps=max_steps,
                            reasoning_effort=reasoning_effort,
                            stream_output=stream_output,
                        )
                        asyncio.run(
                            agent.run(task_description=session.task_description)
                        )
    except Exception as exc:
        log_error_event(
            "agent_error",
            f"Agent run failed for session {session.session_id}: {exc}",
            session_id=session.session_id,
            agent_type=agent_type,
            model=model,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise

    session.end_session()
    log_event(
        "agent_end",
        f"Agent run completed for session {session.session_id}",
        session_id=session.session_id,
        agent_type=agent_type,
    )
    if agent_type == "cli.codex" and stream_output:
        print(f"\nDone. Results saved to {session.session_dir}\n", flush=True)
