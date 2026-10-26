"""Run troubleshooting agents inside a sandbox runtime.

Runtime-specific work (creating the sandbox, credentials, network access,
command execution) goes through :class:`~nika.agents.sandbox.backend.SandboxBackend`.

The agent sees only its workspace: ``task.json`` (:class:`agent.runtime.task.Task`),
its code, and the standard model client variables. It runs one phase per
process (``python -m agent.runtime <phase>``); the host freezes the diagnosis
report and advances the MCP gateway in between.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.runtime.contract import (
    ENV_WORKSPACE,
    REPORT_FILENAME,
    SUBMISSION_CONTEXT_FILENAME,
)
from agent.runtime.task import LlmClientSettings, Task
from agent.utils.loggers import MESSAGES_FILENAME, MessageLogger
from nika.agents.registry import AgentSpec, get_agent_spec
from nika.agents.sandbox.backend import SandboxBackend, get_backend
from nika.agents.sandbox.config import SandboxConfig
from nika.agents.sandbox.env import format_env_for_log
from nika.agents.sandbox.mcp_manifest import build_sandbox_mcp_servers
from nika.agents.sandbox.model_calls import MODEL_CALLS_FILENAME, ModelCallLog
from nika.agents.sandbox.wheels import runner_path_prefix
from nika.agents.sandbox.workspace import (
    TraceMirror,
    cleanup_workspace,
    collect_artifacts,
    opaque_agent_workspace_dir,
    prepare_workspace,
    read_workspace_text,
    trace_mirror,
    write_results_manifest,
    write_workspace_text,
)
from nika.telemetry import telemetry_for_session
from nika.utils.agent_session_id import resolve_agent_session_id
from nika.utils.logger import elapsed_ms, log_event
from nika.utils.session import Session

if TYPE_CHECKING:
    from nika.agents.submission import ReportMapper

logger = logging.getLogger(__name__)

# The Claude Agent SDK waits for the bundled CLI's ``initialize`` reply
# for CLAUDE_CODE_STREAM_CLOSE_TIMEOUT ms. Default is 60s and on slow hosts
# it can easily exceed, causing a failure. We bump it to 5min.
_CLAUDE_SDK_AGENT_TYPES = frozenset({"sdk.claude_sdk", "community.sade"})
_CLAUDE_SDK_INIT_TIMEOUT_MS = 300_000

# Agent code in the workspace (``<workspace>/agent``, ``<workspace>/project``).
AGENT_PACKAGE_DIRNAME = "agent"
AGENT_PROJECT_DIRNAME = "project"
# Project files the agent never sees: its NIKA manifest, secrets, and results.
_PROJECT_HOST_ONLY = ("nika-agent.yaml", ".env", ".env.*", "results", ".nika_cache")
_RUNNER_PID_FILE = "/tmp/agent-runner.pid"
# SIGKILL the runner recorded in _RUNNER_PID_FILE and all its descendants.
_KILL_RUNNER_TREE = (
    "python3 - <<'EOF'\n"
    "import os, signal\n"
    f"root = int(open({_RUNNER_PID_FILE!r}).read())\n"
    "children = {}\n"
    "for pid in filter(str.isdigit, os.listdir('/proc')):\n"
    "    try:\n"
    "        stat = open(f'/proc/{pid}/stat').read()\n"
    "    except OSError:\n"
    "        continue\n"
    "    ppid = int(stat.rsplit(')', 1)[1].split()[1])\n"
    "    children.setdefault(ppid, []).append(int(pid))\n"
    "tree, todo = [], [root]\n"
    "while todo:\n"
    "    pid = todo.pop()\n"
    "    tree.append(pid)\n"
    "    todo += children.get(pid, [])\n"
    "for pid in tree:\n"
    "    try:\n"
    "        os.kill(pid, signal.SIGKILL)\n"
    "    except OSError:\n"
    "        pass\n"
    "EOF"
)


@dataclass
class SandboxRunResult:
    returncode: int
    sandbox_name: str


@dataclass
class SandboxSession:
    # Backend sandbox id (the sbx sandbox name for Docker Sandboxes).
    sandbox_name: str
    workspace_dir: Path
    gateway_port: int
    agent_session_id: str = ""
    trace_mirror: TraceMirror | None = None
    # Host record of the agent's model calls; enforces the LLM request budget.
    call_log: ModelCallLog | None = None
    # Shell prefix that selects the agent's Python (its venv, if it has one).
    python_prefix: str = ""


def _run_settings() -> tuple[LlmClientSettings, float, bool]:
    """``(llm client, MCP read timeout, skills)`` from the run config.

    The agent's client also carries ``agent.temperature``/``top_p``/``seed``.
    """
    from nika.agents.provider_env import host_llm_client
    from nika.run_config.loader import get_run_config

    config = get_run_config()
    return (
        replace(host_llm_client(), **config.agent.sampling_params()),
        float(config.nika.mcp.read_timeout_sec),
        bool(config.agent.enable_skills),
    )


class SandboxManager:
    """Create a sandbox per session and run an agent in it."""

    def __init__(
        self, config: SandboxConfig, backend: SandboxBackend | None = None
    ) -> None:
        self.config = config
        self.backend = backend if backend is not None else get_backend(config)

    def build_task(
        self,
        *,
        session: Session,
        spec: AgentSpec,
        model: str,
        max_steps: int | None,
        reasoning_effort: str | None,
        max_tokens: int | None,
        llm_provider: str | None,
        mcp_gateway_agent_url: str,
        stream_output: bool,
    ) -> Task:
        """The agent's task: run settings and the session's MCP servers.

        It names no host path: the host ``session_dir`` is a filesystem
        shortcut to ``ground_truth.json`` and other host-only artifacts, and
        ``session_id`` is the opaque agent handle only.
        """
        scenario_name = getattr(session, "scenario_name", "") or ""
        backend = getattr(session, "backend", "") or "kathara"
        agent_sid = resolve_agent_session_id(session)
        llm, mcp_read_timeout, enable_skills = _run_settings()
        return Task(
            session_id=agent_sid,
            agent_class=spec.class_path,
            task_description=session.task_description,
            # Bake the session-specific gateway URL into the workspace.
            # Parallel trials share the host process environment, so resolving
            # it later from NIKA_MCP_GATEWAY_* can pick up a sibling's value.
            mcp_servers=build_sandbox_mcp_servers(
                session_id=agent_sid,
                scenario_name=scenario_name,
                backend=backend,
                gateway_agent_url=mcp_gateway_agent_url.rstrip("/"),
                telemetry=telemetry_for_session(session),
            ),
            model=model,
            llm_provider=llm_provider,
            max_steps=max_steps if max_steps is not None else 20,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            stream_output=stream_output,
            scenario_name=scenario_name,
            llm=llm,
            mcp_read_timeout_sec=mcp_read_timeout,
            enable_skills=enable_skills,
        )

    @staticmethod
    def _import_roots(spec: AgentSpec) -> list[str]:
        """Workspace directories on the agent's ``PYTHONPATH``."""
        return [""] + ([AGENT_PROJECT_DIRNAME] if spec.import_root else [])

    @staticmethod
    def _bundle_agent_sources(workspace_dir: Path, spec: AgentSpec) -> None:
        """Copy the agent package and an added agent's project into the workspace."""
        import agent

        shutil.copytree(
            Path(agent.__file__).resolve().parent,
            workspace_dir / AGENT_PACKAGE_DIRNAME,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )
        if spec.import_root:
            shutil.copytree(
                spec.import_root,
                workspace_dir / AGENT_PROJECT_DIRNAME,
                ignore=shutil.ignore_patterns(
                    *_PROJECT_HOST_ONLY, ".venv", ".git", "__pycache__", "*.pyc"
                ),
                symlinks=True,
            )

    def _runtime_env(
        self,
        *,
        spec: AgentSpec,
        model: str,
        llm_provider: str | None,
        credential_env: dict[str, str],
        sandbox_workspace: str,
    ) -> dict[str, str]:
        """Variables of the agent process (see :mod:`agent.runtime.__main__`)."""
        env = {
            ENV_WORKSPACE: sandbox_workspace,
            "PYTHONPATH": ":".join(
                f"{sandbox_workspace}/{root}".rstrip("/")
                for root in self._import_roots(spec)
            ),
        }
        if spec.id in _CLAUDE_SDK_AGENT_TYPES:
            env["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = str(_CLAUDE_SDK_INIT_TIMEOUT_MS)
        if llm_provider == "custom" and spec.client_family(llm_provider) == "anthropic":
            from nika.agents.provider_env import custom_model_claude_env

            env.update(custom_model_claude_env(model))
        env.update(credential_env)
        return env

    @contextmanager
    def open_session(
        self,
        *,
        session: Session,
        agent_type: str,
        model: str,
        max_steps: int | None,
        reasoning_effort: str | None,
        max_tokens: int | None,
        llm_provider: str | None,
        mcp_gateway_agent_url: str,
        gateway_port: int,
        stream_output: bool,
    ) -> Iterator[SandboxSession]:
        backend = self.backend
        backend.ensure_available()
        spec = get_agent_spec(agent_type)
        agent_sid = resolve_agent_session_id(session)
        session_dir = Path(session.session_dir).resolve()

        try:
            call_log = ModelCallLog(
                session_dir / MODEL_CALLS_FILENAME,
                max_requests=max_steps if max_steps is not None else 20,
            )
            credential_env = backend.prepare_host(
                agent_type=agent_type, llm_provider=llm_provider, call_log=call_log
            )
            sandbox_gateway_url = backend.sandbox_gateway_url(
                port=gateway_port, gateway_url=mcp_gateway_agent_url
            )
            task = self.build_task(
                session=session,
                spec=spec,
                model=model,
                max_steps=max_steps,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                llm_provider=llm_provider,
                mcp_gateway_agent_url=sandbox_gateway_url,
                stream_output=stream_output,
            )

            # Workspace path is chosen first so PYTHONPATH matches the path the
            # sandbox sees.
            workspace_path = opaque_agent_workspace_dir(agent_sid)
            runtime_env = self._runtime_env(
                spec=spec,
                model=model,
                llm_provider=llm_provider,
                credential_env=credential_env,
                sandbox_workspace=backend.workspace_path(workspace_path),
            )
            workspace = prepare_workspace(
                session_dir=session_dir,
                task_json=task.to_json(),
                runtime_env=runtime_env,
                agent_session_id=agent_sid,
                enable_skills=task.enable_skills,
                workspace_dir=workspace_path,
            )
            self._bundle_agent_sources(workspace.workspace_dir, spec)
            mirror = trace_mirror(workspace)
            backend.stage_workspace(workspace.workspace_dir, agent_type=agent_type)

            # Lifetime covers create → agent run → teardown; setup span is logged
            # on sandbox_start once the sandbox is ready (env_start-style).
            lifetime_started = time.perf_counter()
            sandbox_id: str | None = None
            try:
                sandbox_id = backend.create(
                    agent_session_id=agent_sid,
                    agent_type=agent_type,
                    workspace_dir=workspace.workspace_dir,
                )
                backend.allow_gateway(
                    sandbox_id, port=gateway_port, gateway_url=mcp_gateway_agent_url
                )
                log_event(
                    "sandbox_start",
                    f"Created {backend.name} sandbox {sandbox_id} "
                    f"for session {session.session_id}",
                    session_id=session.session_id,
                    agent_session_id=agent_sid,
                    agent_type=agent_type,
                    sandbox_backend=backend.name,
                    sandbox_name=sandbox_id,
                    mcp_gateway=mcp_gateway_agent_url,
                    env=format_env_for_log(runtime_env),
                    duration_ms=elapsed_ms(lifetime_started),
                    **backend.start_log_fields(),
                )
                # Runtimes without a shared workspace fetch the trace first.
                mirror.refresh = lambda sid=sandbox_id: backend.pull_files(
                    sid, workspace.workspace_dir, [MESSAGES_FILENAME]
                )
                mirror.start()
                yield SandboxSession(
                    sandbox_name=sandbox_id,
                    workspace_dir=workspace.workspace_dir,
                    gateway_port=gateway_port,
                    agent_session_id=agent_sid,
                    trace_mirror=mirror,
                    call_log=call_log,
                    python_prefix=runner_path_prefix(spec.requires_python),
                )
            finally:
                try:
                    mirror.stop()
                    if sandbox_id is not None:
                        backend.revoke_gateway(
                            sandbox_id,
                            port=gateway_port,
                            gateway_url=mcp_gateway_agent_url,
                        )
                finally:
                    try:
                        if sandbox_id is not None and not self.config.keep_container:
                            backend.remove(sandbox_id)
                    finally:
                        try:
                            collect_artifacts(workspace)
                        finally:
                            # Keep the task even when artifact collection or
                            # earlier sandbox cleanup fails.
                            write_results_manifest(session_dir, task.to_json())
                            # A kept sandbox needs its workspace; session close
                            # removes both.
                            if not self.config.keep_container:
                                cleanup_workspace(workspace)
                            log_event(
                                "sandbox_end",
                                f"{backend.name} sandbox finished "
                                f"for session {session.session_id}",
                                session_id=session.session_id,
                                agent_type=agent_type,
                                sandbox_backend=backend.name,
                                sandbox_name=sandbox_id or "",
                                duration_ms=elapsed_ms(lifetime_started),
                            )
        finally:
            backend.release_host()

    def _kill_runner_tree(self, sandbox_name: str) -> None:
        """Kill the phase runner and every process it started in the sandbox.

        Killing the host side of the command stream does not reach processes
        the runner spawned (agent CLIs, sub-agents); left running, they would
        spend the next phase's budget.
        """
        try:
            killer = self.backend.exec_stream(sandbox_name, _KILL_RUNNER_TREE)
            for _ in killer.stdout or ():
                pass
            killer.wait()
        except Exception as exc:  # noqa: BLE001 - the stream kill still follows
            logger.warning("Could not stop the agent processes: %s", exc)

    def _run_step(
        self,
        *,
        sandbox_session: SandboxSession,
        phase: str,
        stream_output: bool,
        timeout_sec: float | None,
        budget_sec: int,
    ) -> bool:
        """Run one pipeline phase of the agent inside the sandbox.

        Returns ``True`` when the phase used up its LLM request budget: the
        model proxy refused a request, and the host stopped the agent.
        """
        workspace = self.backend.workspace_path(sandbox_session.workspace_dir)
        inner = (
            f"cd {shlex.quote(workspace)} && echo $$ > {_RUNNER_PID_FILE} && "
            f"{sandbox_session.python_prefix}PYTHONPATH={shlex.quote(workspace)} "
            f"exec python3 -m agent.runtime {phase}"
        )
        sandbox_name = sandbox_session.sandbox_name
        proc = self.backend.exec_stream(sandbox_name, inner)
        assert proc.stdout is not None
        timed_out = threading.Event()
        call_log = sandbox_session.call_log
        finished = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            proc.kill()

        def _kill_on_budget() -> None:
            # Agents handle a refused model request differently (retry, crash,
            # or wrap up); stop every one the same way.
            while not finished.is_set():
                if call_log.exhausted.wait(0.5):
                    self._kill_runner_tree(sandbox_name)
                    proc.kill()
                    return

        if call_log is not None:
            threading.Thread(
                target=_kill_on_budget, name="budget-watch", daemon=True
            ).start()

        timer = (
            threading.Timer(max(timeout_sec, 0.0), _kill_on_timeout)
            if timeout_sec is not None
            else None
        )
        if timer is not None:
            timer.start()
        captured: list[str] = []
        try:
            for line in proc.stdout:
                captured.append(line)
                if stream_output:
                    sys.stdout.write(line)
                    sys.stdout.flush()
            returncode = proc.wait()
        finally:
            finished.set()
            if timer is not None:
                timer.cancel()
        if timed_out.is_set():
            from nika.agents.registry import AgentTimeoutError

            raise AgentTimeoutError(
                f"agent run exceeded agent.timeout_sec ({budget_sec}s)"
            )
        if call_log is not None and call_log.exhausted.is_set():
            return True
        if returncode != 0:
            detail = "".join(captured).strip() or "(no output)"
            raise RuntimeError(
                f"Agent runner ({phase}) exited with code {returncode}:\n{detail}"
            )
        return False

    def _run_in_sandbox(
        self,
        *,
        sandbox_session: SandboxSession,
        stream_output: bool,
        timeout_sec: int,
        session: Session,
        report_mapper: ReportMapper | None = None,
    ) -> None:
        """Diagnosis in the sandbox, freeze + phase advance on the host, then submission.

        The gateway's phase-advance secret stays in this host process, so the
        sandboxed agent cannot reach the submission context before its
        diagnosis step has exited. With a *report_mapper* (unstructured
        submission), the host maps the report and the sandbox stays idle.
        """
        from nika.agents.mcp_config import begin_submission_mcp_phase

        deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None

        def remaining() -> float | None:
            return None if deadline is None else deadline - time.monotonic()

        sandbox_id = sandbox_session.sandbox_name
        workspace = sandbox_session.workspace_dir
        mirror = sandbox_session.trace_mirror
        call_log = sandbox_session.call_log
        exhausted = self._run_step(
            sandbox_session=sandbox_session,
            phase=DIAGNOSIS,
            stream_output=stream_output,
            timeout_sec=remaining(),
            budget_sec=timeout_sec,
        )
        if exhausted:
            report = self._budget_report(
                sandbox_session, session_dir=session.session_dir
            )
            # The submission step reads the frozen report from the workspace.
            write_workspace_text(
                workspace / REPORT_FILENAME,
                json.dumps({"report": report}, ensure_ascii=False),
            )
            self.backend.push_files(sandbox_id, workspace, [REPORT_FILENAME])
        else:
            self.backend.pull_files(sandbox_id, workspace, [REPORT_FILENAME])
            report_path = workspace / REPORT_FILENAME
            try:
                report = json.loads(read_workspace_text(report_path))["report"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    f"Agent diagnosis step left no report: {exc}"
                ) from exc
            if not isinstance(report, str):
                raise RuntimeError("Agent diagnosis step left a non-text report")
        if mirror is not None:
            mirror.set_phase(SUBMISSION)
        if call_log is not None:
            call_log.set_phase(SUBMISSION)
        context = begin_submission_mcp_phase(sandbox_session.agent_session_id, report)
        if report_mapper is not None:
            self._map_report_on_host(
                report_mapper,
                report,
                context,
                session=session,
                agent_session_id=sandbox_session.agent_session_id,
                stream_output=stream_output,
                timeout_sec=remaining(),
                budget_sec=timeout_sec,
            )
            return
        write_workspace_text(
            workspace / SUBMISSION_CONTEXT_FILENAME,
            json.dumps(context, ensure_ascii=False),
        )
        self.backend.push_files(sandbox_id, workspace, [SUBMISSION_CONTEXT_FILENAME])
        if self._run_step(
            sandbox_session=sandbox_session,
            phase=SUBMISSION,
            stream_output=stream_output,
            timeout_sec=remaining(),
            budget_sec=timeout_sec,
        ):
            # submit() records the answer when called; a missing one is scored
            # as no submission.
            self._log_budget_stop(
                sandbox_session, SUBMISSION, session.session_dir
            ).log_agent_done(stopped_at_budget=True)

    @staticmethod
    def _log_budget_stop(
        sandbox_session: SandboxSession, phase: str, session_dir: str
    ) -> MessageLogger:
        """Record in the host trace that the host stopped *phase* at its budget."""
        if sandbox_session.trace_mirror is not None:
            sandbox_session.trace_mirror.close_phase_on_host(phase)
        logger = MessageLogger(phase=phase, session_dir=session_dir)
        call_log = sandbox_session.call_log
        logger.log(
            "max_steps_reached",
            {
                "max_steps": call_log.max_requests if call_log else None,
                "enforced_by": "model_proxy",
            },
        )
        return logger

    def _budget_report(
        self, sandbox_session: SandboxSession, *, session_dir: str
    ) -> str:
        """Diagnosis report of a phase stopped at its LLM request budget.

        As for agents that stop themselves: the latest assistant text, from the
        host's own record of the model replies.
        """
        logger = self._log_budget_stop(sandbox_session, DIAGNOSIS, session_dir)
        call_log = sandbox_session.call_log
        text = call_log.last_text(DIAGNOSIS).strip() if call_log else ""
        if not text:
            logger.log_agent_error(
                "diagnosis phase used its whole LLM request budget "
                "without an assistant report"
            )
            raise RuntimeError(
                "ERROR: diagnosis phase reached max_steps without an assistant report"
            )
        logger.log_agent_done(report_length=len(text))
        return text

    @staticmethod
    def _map_report_on_host(
        report_mapper: ReportMapper,
        report: str,
        context: dict,
        *,
        session: Session,
        agent_session_id: str,
        stream_output: bool,
        timeout_sec: float | None,
        budget_sec: int,
    ) -> None:
        import asyncio

        from agent.base import run_submission_phase
        from nika.agents.registry import AgentTimeoutError

        async def _run() -> bool:
            """Return ``False`` when the run budget expired."""
            try:
                async with asyncio.timeout(timeout_sec) as budget:
                    await run_submission_phase(
                        lambda: report_mapper.submit(
                            report,
                            context,
                            session_id=agent_session_id,
                            scenario_name=session.scenario_name,
                            trace_dir=session.session_dir,
                        ),
                        trace_dir=session.session_dir,
                        stream_output=stream_output,
                        **report_mapper.trace_fields(),
                    )
            except TimeoutError:
                # An LLM request timeout is not a budget expiry.
                if not budget.expired():
                    raise
                return False
            return True

        if not asyncio.run(_run()):
            raise AgentTimeoutError(
                f"agent run exceeded agent.timeout_sec ({budget_sec}s)"
            )

    def run(
        self,
        *,
        session: Session,
        agent_type: str,
        model: str,
        max_steps: int | None,
        timeout_sec: int,
        reasoning_effort: str | None,
        max_tokens: int | None,
        llm_provider: str | None,
        mcp_gateway_agent_url: str,
        gateway_port: int,
        stream_output: bool = True,
        report_mapper: ReportMapper | None = None,
    ) -> SandboxRunResult:
        """Create a sandbox, run the agent, collect artifacts."""
        with self.open_session(
            session=session,
            agent_type=agent_type,
            model=model,
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            llm_provider=llm_provider,
            mcp_gateway_agent_url=mcp_gateway_agent_url,
            gateway_port=gateway_port,
            stream_output=stream_output,
        ) as sandbox_session:
            self._run_in_sandbox(
                sandbox_session=sandbox_session,
                stream_output=stream_output,
                timeout_sec=timeout_sec,
                session=session,
                report_mapper=report_mapper,
            )

        return SandboxRunResult(returncode=0, sandbox_name=sandbox_session.sandbox_name)
