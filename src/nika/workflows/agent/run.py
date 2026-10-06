"""Run a troubleshooting agent against the current session task."""

import logging
import time

from agent.registry import create_agent, run_agent
from agent.sandbox import SANDBOX_SUPPORTED_AGENTS, SbxSandboxManager, sbx_available
from agent.sandbox.config import resolve_sandbox_config, sandbox_gateway_agent_host
from agent.utils.provider_env import provider_env_context
from nika.mcp.gateway.lifecycle import mcp_gateway_for_session
from nika.utils.agent_config import (
    resolve_agent_model,
    resolve_agent_timeout,
    resolve_agent_type,
    resolve_llm_provider,
    resolve_max_steps,
    resolve_max_tokens,
    resolve_reasoning_effort,
)
from nika.utils.logger import bind_session_dir, elapsed_ms, log_error_event, log_event
from nika.utils.session import Session

logging.basicConfig(level=logging.INFO)


def start_agent(
    agent_type: str | None = None,
    llm_provider: str | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    *,
    session_id: str | None = None,
    reasoning_effort: str | None = None,
    stream_output: bool = True,
    check_fault_presence: bool = False,
    sandbox_keep_container: bool | None = None,
    sandbox_cpus: int | None = None,
    sandbox_memory: str | None = None,
    sandbox_offline_sdk_wheels: bool | None = None,
) -> None:
    """Load the running session, run the agent on ``task_description``, then end the session."""
    from nika.utils.agent_config import apply_custom_provider_env

    # Benchmarks pass stream_output=False; keep MCP/httpx off the console.
    if not stream_output:
        from nika.workflows.benchmark.display import quiet_third_party_logging

        quiet_third_party_logging()

    apply_custom_provider_env()

    agent_type = resolve_agent_type(agent_type)
    max_steps = resolve_max_steps(max_steps)
    timeout_sec = resolve_agent_timeout()
    reasoning_effort = resolve_reasoning_effort(reasoning_effort)
    max_tokens = resolve_max_tokens(agent_type)
    llm_provider = resolve_llm_provider(llm_provider, agent_type=agent_type)
    model = resolve_agent_model(agent_type, model)
    sandbox_config = resolve_sandbox_config(
        keep_container=sandbox_keep_container,
        cpus=sandbox_cpus,
        memory=sandbox_memory,
        offline_sdk_wheels=sandbox_offline_sdk_wheels,
    )
    use_sandbox = agent_type in SANDBOX_SUPPORTED_AGENTS
    if use_sandbox:
        from agent.sandbox.sbx.images import ensure_sbx_template_images

        # Idempotent: skips pull when the template is already local (e.g. lab
        # deploy / benchmark job preload). Covers ``nika agent run`` without
        # a prior env start that knew the sandbox agent.
        ensure_sbx_template_images([agent_type])

    session = Session()
    session.load_running_session(session_id=session_id)
    session.update_session("agent_type", agent_type)
    if llm_provider is not None:
        session.update_session("llm_provider", llm_provider)
    session.update_session("model", model)
    if reasoning_effort is not None:
        session.update_session("reasoning_effort", reasoning_effort)
    if max_tokens is not None:
        session.update_session("max_tokens", max_tokens)
    session.start_session()

    bind_session_dir(session.session_dir)
    # Re-apply after agent imports may call ``basicConfig(INFO)``.
    if not stream_output:
        from nika.workflows.benchmark.display import quiet_third_party_logging

        quiet_third_party_logging()

    # Session lifecycle (nika.jsonl): agent_start → agent_end | agent_error.
    # Phase bookends (messages.jsonl) use agent_start → agent_done | agent_error.
    log_event(
        "agent_start",
        f"Starting agent: {agent_type} (model={model}) in session {session.session_id}"
        + (" [sandbox]" if use_sandbox else ""),
        session_id=session.session_id,
        agent_type=agent_type,
        model=model,
        sandbox=use_sandbox,
        max_steps=max_steps,
        timeout_sec=timeout_sec,
    )
    from nika.validation.presence import PresenceWatch

    # Benchmark inject and agent execution share the injecting instance. Standalone
    # session commands run in separate processes and cannot use that instance.
    presence = (
        PresenceWatch(session.session_id, session.session_dir)
        if check_fault_presence
        else None
    )
    if presence is not None:
        presence.start()
    agent_started = time.perf_counter()
    agent_exc: BaseException | None = None
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
                        timeout_sec=timeout_sec,
                        reasoning_effort=reasoning_effort,
                        max_tokens=max_tokens,
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
                            max_tokens=max_tokens,
                            stream_output=stream_output,
                        )
                        run_agent(
                            agent,
                            session.task_description,
                            timeout_sec=timeout_sec,
                        )
            # Pull remote-written artifacts (e.g. submission.json) after the agent.
            pull_session_artifacts(session.session_id, session.session_dir)
        else:
            with mcp_gateway_for_session(
                session.session_id,
                scenario_name=session.scenario_name,
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
                    gateway_agent_url = gateway_manager.agent_url
                    if not gateway_agent_url:
                        raise RuntimeError(
                            "MCP gateway agent URL was not set for sandbox execution"
                        )
                    SbxSandboxManager(sandbox_config).run(
                        session=session,
                        agent_type=agent_type,
                        model=model,
                        max_steps=max_steps,
                        timeout_sec=timeout_sec,
                        reasoning_effort=reasoning_effort,
                        max_tokens=max_tokens,
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
                            max_tokens=max_tokens,
                            stream_output=stream_output,
                        )
                        run_agent(
                            agent,
                            session.task_description,
                            timeout_sec=timeout_sec,
                        )
    except BaseException as exc:
        agent_exc = exc
        if not isinstance(exc, (KeyboardInterrupt, SystemExit)):
            log_error_event(
                "agent_error",
                f"Agent run failed for session {session.session_id}: {exc}",
                session_id=session.session_id,
                agent_type=agent_type,
                model=model,
                error=str(exc),
                error_type=type(exc).__name__,
                duration_ms=elapsed_ms(agent_started),
            )
    if isinstance(agent_exc, (KeyboardInterrupt, SystemExit)):
        # The parent kills the worker after a short grace period, so lab
        # cleanup must not wait for artifact reads.
        if presence is not None:
            presence.cancel()
        raise agent_exc
    if presence is not None:
        presence.finish()
    if agent_exc is not None:
        raise agent_exc

    session.end_session()
    log_event(
        "agent_end",
        f"Agent run completed for session {session.session_id}",
        session_id=session.session_id,
        agent_type=agent_type,
        duration_ms=elapsed_ms(agent_started),
    )
    if agent_type == "cli.codex" and stream_output:
        print(f"\nDone. Results saved to {session.session_dir}\n", flush=True)
