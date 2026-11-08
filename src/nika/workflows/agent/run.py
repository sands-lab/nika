"""Run a troubleshooting agent against the current session task."""

import logging
import time
from contextlib import ExitStack
from urllib.parse import urlparse

from nika.agents.registry import create_agent, get_agent_spec, run_agent
from nika.agents.sandbox import SandboxManager
from nika.agents.sandbox.backend import backend_class, get_backend
from nika.agents.sandbox.config import resolve_sandbox_config
from nika.agents.mcp_config import engine_gateway
from nika.benchmark_engine.protocol import AgentAccess, GatewayRequest
from nika.utils.agent_config import (
    resolve_agent_model,
    resolve_agent_timeout,
    resolve_agent_type,
    resolve_llm_provider,
    resolve_max_steps,
    resolve_max_tokens,
    resolve_reasoning_effort,
    resolve_sampling,
    resolve_submission_settings,
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
    task_description: str | None = None,
    reasoning_effort: str | None = None,
    stream_output: bool = True,
    check_fault_presence: bool = False,
    sandbox_keep_container: bool | None = None,
    sandbox_cpus: int | None = None,
    sandbox_memory: str | None = None,
    sandbox_offline_sdk_wheels: bool | None = None,
    access: AgentAccess | None = None,
) -> None:
    """Load the running session, run the agent on ``task_description``, then end the session.

    Every agent runs in its own sandbox; only test agents that need host state
    (``template=None``, e.g. ``mock``) run in this process. *access* is the
    release's access policy; without it, the run config's ``agent.access.role``
    selects one. A benchmark trial passes the *task_description* that the
    engine's ``begin`` returned; a hands-on session uses its own.
    """
    from nika.agents.submission import ReportMapper
    from nika.run_config.loader import get_run_config
    from nika.run_config.schema import AgentSubmissionSettings
    from nika.utils.agent_config import apply_custom_provider_env

    # Benchmarks pass stream_output=False; keep MCP/httpx off the console.
    if not stream_output:
        from nika.workflows.benchmark.display import quiet_third_party_logging

        quiet_third_party_logging()

    apply_custom_provider_env()

    agent_type = resolve_agent_type(agent_type)
    max_steps = resolve_max_steps(max_steps)
    timeout_sec = resolve_agent_timeout()
    reasoning_effort = resolve_reasoning_effort(agent_type, reasoning_effort)
    max_tokens = resolve_max_tokens(agent_type)
    llm_provider = resolve_llm_provider(llm_provider, agent_type=agent_type)
    model = resolve_agent_model(agent_type, model)
    submission = resolve_submission_settings()
    if get_agent_spec(agent_type).template is None:
        # Host-run agents (mock, oracle) answer from the ground truth themselves.
        submission = AgentSubmissionSettings()
    # Built before the agent's provider environment clears other credentials.
    report_mapper = ReportMapper.from_settings(
        submission, max_tokens=get_run_config().agent.max_tokens
    )
    sandbox_config = resolve_sandbox_config(
        keep_container=sandbox_keep_container,
        cpus=sandbox_cpus,
        memory=sandbox_memory,
        offline_sdk_wheels=sandbox_offline_sdk_wheels,
    )
    use_sandbox = get_agent_spec(agent_type).template is not None
    if use_sandbox:
        # Idempotent: a no-op when the job or env start already prepared it.
        # Covers ``nika agent run`` without a prior env start that knew the
        # sandbox agent.
        images = get_backend(sandbox_config).prepare_templates([agent_type])
        from nika.net_env.utils.kathara.docker_files.docker_images import (
            ensure_nika_docker_images,
        )

        ensure_nika_docker_images(images)

    session = Session()
    session.load_running_session(session_id=session_id)
    if task_description is None:
        task_description = session.task_description
    session.update_session("agent_type", agent_type)
    if llm_provider is not None:
        session.update_session("llm_provider", llm_provider)
    session.update_session("model", model)
    if use_sandbox:
        session.update_session("sandbox_backend", sandbox_config.backend)
        fields = backend_class(sandbox_config.backend).session_fields(sandbox_config)
        for key, value in fields.items():
            session.update_session(key, value)
    if reasoning_effort is not None:
        session.update_session("reasoning_effort", reasoning_effort)
    if max_tokens is not None:
        session.update_session("max_tokens", max_tokens)
    session.update_session("submission", submission.identity())
    sampling = resolve_sampling(agent_type)
    if sampling is not None:
        session.update_session("sampling", sampling)
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
        submission_mode=submission.mode,
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
    if stream_output:
        effort_line = (
            f" | Reasoning effort: {reasoning_effort}" if reasoning_effort else ""
        )
        mode_line = " | Sandbox: enabled" if use_sandbox else ""
        print(
            f"Session {session.session_id}\n"
            f"Agent: {agent_type} | Model: {model}{effort_line}{mode_line}\n"
            f"Results: {session.session_dir}\n",
            flush=True,
        )
    try:
        from nika.remote.config import is_remote_enabled

        sandbox_backend = backend_class(sandbox_config.backend)
        with ExitStack() as stack:
            endpoints = stack.enter_context(
                engine_gateway(
                    session.session_id,
                    session.session_dir,
                    GatewayRequest(
                        access=access,
                        tool_call_limits=get_run_config().agent.tool_call_limits
                        or None,
                        sandbox=use_sandbox,
                        bind_host=(
                            sandbox_backend.gateway_bind_host if use_sandbox else None
                        ),
                        agent_host=sandbox_backend.gateway_agent_host,
                    ),
                )
            )
            if is_remote_enabled():
                from nika.remote.workflows import follow_remote_logs

                stack.enter_context(
                    follow_remote_logs(session.session_id, session.session_dir)
                )
            if use_sandbox:
                SandboxManager(sandbox_config).run(
                    session=session,
                    task_description=task_description,
                    agent_type=agent_type,
                    model=model,
                    max_steps=max_steps,
                    timeout_sec=timeout_sec,
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                    llm_provider=llm_provider,
                    mcp_gateway_agent_url=endpoints.agent_url,
                    # The port this host dials; a NIKA Remote client may map it
                    # (e.g. a local forwarder to a lab VM).
                    gateway_port=int(urlparse(endpoints.agent_url).port or 0),
                    stream_output=stream_output,
                    report_mapper=report_mapper,
                )
            else:
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
                    task_description,
                    timeout_sec=timeout_sec,
                    report_mapper=report_mapper,
                )
        if is_remote_enabled():
            from nika.remote.workflows import pull_session_artifacts

            # Pull remote-written artifacts (e.g. submission.json) after the agent.
            pull_session_artifacts(session.session_id, session.session_dir)
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
    if stream_output:
        print(f"\nDone. Results saved to {session.session_dir}\n", flush=True)
