"""Run troubleshooting agents inside a sandbox runtime.

Runtime-specific work (creating the sandbox, credentials, network access,
command execution) goes through :class:`~agent.sandbox.backend.SandboxBackend`.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.sandbox.backend import (
    SandboxBackend,
    get_backend,
    register_active_sandbox,
    release_active_sandbox,
)
from agent.sandbox.config import ENV_SANDBOX_NAME, ENV_SESSION_DIR, SandboxConfig
from agent.sandbox.constants import (
    CREDENTIAL_ENV_KEYS,
    DIAGNOSIS_REPORT_FILENAME,
    MANIFEST_FILENAME,
    SDK_AGENT_TYPES,
    SUBMISSION_CONTEXT_FILENAME,
)
from agent.sandbox.env import format_env_for_log
from agent.sandbox.mcp_manifest import build_sandbox_mcp_servers
from agent.sandbox.workspace import (
    TraceMirror,
    cleanup_workspace,
    collect_artifacts,
    opaque_agent_workspace_dir,
    prepare_workspace,
    read_workspace_text,
    trace_mirror,
    write_workspace_text,
)
from agent.utils.loggers import MESSAGES_FILENAME
from agent.utils.skills import ENV_ENABLE_SKILLS, skills_enabled
from nika.utils.agent_session_id import resolve_agent_session_id
from nika.utils.logger import elapsed_ms, log_event
from nika.utils.session import Session

logger = logging.getLogger(__name__)

# The Claude Agent SDK waits for the bundled CLI's ``initialize`` reply
# for CLAUDE_CODE_STREAM_CLOSE_TIMEOUT ms. Default is 60s and on slow hosts
# it can easily exceed, causing a failure. We bump it to 5min.
_CLAUDE_SDK_AGENT_TYPES = frozenset({"sdk.claude_sdk", "community.sade"})
_CLAUDE_SDK_INIT_TIMEOUT_MS = 300_000

# Host-side CLI sandboxes read ``NIKA_SANDBOX_NAME`` / ``NIKA_SESSION_DIR``
# from process env. Concurrent sessions in one process must not interleave
# save/restore of those keys (benchmark parallel batches use spawn workers;
# this lock still protects in-process callers and nested contexts).
_sandbox_env_lock = threading.RLock()


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
    # In-VM SDK agents only: copies the workspace trace into the host trace.
    trace_mirror: TraceMirror | None = None


class SandboxManager:
    """Create a sandbox per session and run a NIKA agent against it."""

    def __init__(
        self, config: SandboxConfig, backend: SandboxBackend | None = None
    ) -> None:
        self.config = config
        self.backend = backend if backend is not None else get_backend(config)

    def write_manifest(
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
        stream_output: bool,
    ) -> dict:
        gateway_url = mcp_gateway_agent_url.rstrip("/")
        scenario_name = getattr(session, "scenario_name", "")
        backend = getattr(session, "backend", "") or "kathara"
        agent_sid = resolve_agent_session_id(session)
        # Do not bake host session_dir into the sandbox: it is a filesystem
        # shortcut to ground_truth.json and other host-only artifacts.
        # session_id in the manifest is the opaque agent handle only.
        manifest = {
            "session_id": agent_sid,
            "agent_type": agent_type,
            "model": model,
            "max_steps": max_steps,
            "reasoning_effort": reasoning_effort,
            "max_tokens": max_tokens,
            "llm_provider": llm_provider,
            "task_description": session.task_description,
            "scenario_name": scenario_name,
            "backend": backend,
            "mcp_gateway_agent_url": gateway_url,
            "stream_output": stream_output,
        }
        # Bake the session-specific gateway URL into the workspace.  Parallel
        # CLI trials share the host process environment, so resolving this URL
        # later from NIKA_MCP_GATEWAY_* can pick up a sibling trial's value.
        manifest["mcp_servers"] = build_sandbox_mcp_servers(
            session_id=agent_sid,
            scenario_name=scenario_name,
            backend=backend,
            gateway_agent_url=gateway_url,
        )
        return manifest

    def _bundle_agent_sources(self, workspace_dir: Path) -> None:
        """Copy agent code (prompts, SDK workers) into the sandbox workspace."""
        from nika.config import REPO_ROOT

        src = REPO_ROOT / "src" / "agent"
        dst = workspace_dir / "agent"
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(
            src,
            dst,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
        )

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
        agent_sid = resolve_agent_session_id(session)
        session_dir = Path(session.session_dir).resolve()

        try:
            credential_env = backend.prepare_host(
                agent_type=agent_type, llm_provider=llm_provider
            )
            manifest = self.write_manifest(
                session=session,
                agent_type=agent_type,
                model=model,
                max_steps=max_steps,
                reasoning_effort=reasoning_effort,
                max_tokens=max_tokens,
                llm_provider=llm_provider,
                mcp_gateway_agent_url=mcp_gateway_agent_url,
                stream_output=stream_output,
            )

            # Workspace path is chosen before prepare so PYTHONPATH matches the
            # path the sandbox sees.
            workspace_path = opaque_agent_workspace_dir(agent_sid)
            sandbox_workspace = backend.workspace_path(workspace_path)

            runtime_env = {
                "NIKA_SANDBOX_EXECUTION": "1",
                "NIKA_SESSION_ID": agent_sid,
                "NIKA_MCP_GATEWAY_AGENT_URL": mcp_gateway_agent_url.rstrip("/"),
                "PYTHONPATH": f"{sandbox_workspace}/agent",
                ENV_ENABLE_SKILLS: "1" if skills_enabled() else "0",
            }
            lab_backend = getattr(session, "backend", "").strip()
            if lab_backend:
                runtime_env["NIKA_SESSION_BACKEND"] = lab_backend
            if agent_type in _CLAUDE_SDK_AGENT_TYPES:
                runtime_env["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] = str(
                    _CLAUDE_SDK_INIT_TIMEOUT_MS
                )
            runtime_env.update(credential_env)

            workspace = prepare_workspace(
                session_dir=session_dir,
                manifest=manifest,
                runtime_env=runtime_env,
                agent_session_id=agent_sid,
                workspace_dir=workspace_path,
            )
            mirror: TraceMirror | None = None
            if agent_type in SDK_AGENT_TYPES:
                self._bundle_agent_sources(workspace.workspace_dir)
                mirror = trace_mirror(workspace)
            backend.stage_workspace(workspace.workspace_dir, agent_type=agent_type)

            # Lifetime covers create → agent run → teardown; setup span is logged
            # on sandbox_start once the sandbox is ready (env_start-style).
            lifetime_started = time.perf_counter()

            # Serialize host env mutation for the lifetime of this sandbox session so
            # a sibling session cannot restore/clobber our NIKA_SANDBOX_* values.
            _sandbox_env_lock.acquire()
            prior_session_dir = os.environ.get(ENV_SESSION_DIR)
            prior_sandbox_name = os.environ.get(ENV_SANDBOX_NAME)
            prior_runtime_env = {key: os.environ.get(key) for key in runtime_env}
            sandbox_id: str | None = None
            try:
                sandbox_id = backend.create(
                    agent_session_id=agent_sid,
                    agent_type=agent_type,
                    workspace_dir=workspace.workspace_dir,
                )
                register_active_sandbox(sandbox_id, backend)
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
                os.environ[ENV_SANDBOX_NAME] = sandbox_id
                os.environ[ENV_SESSION_DIR] = str(workspace.workspace_dir)
                # Force credential placeholders over host .env API keys so the
                # sandbox never receives real secrets (and DeepSeek set-custom
                # placeholders are not shadowed by host dotenv values).
                for key, value in runtime_env.items():
                    if key in CREDENTIAL_ENV_KEYS:
                        os.environ[key] = value
                    else:
                        os.environ.setdefault(key, value)
                if mirror is not None:
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
                )
            finally:
                try:
                    if mirror is not None:
                        mirror.stop()
                    if sandbox_id is not None:
                        release_active_sandbox(sandbox_id)
                    if prior_sandbox_name is None:
                        os.environ.pop(ENV_SANDBOX_NAME, None)
                    else:
                        os.environ[ENV_SANDBOX_NAME] = prior_sandbox_name
                    if prior_session_dir is None:
                        os.environ.pop(ENV_SESSION_DIR, None)
                    else:
                        os.environ[ENV_SESSION_DIR] = prior_session_dir
                    for key, prior_value in prior_runtime_env.items():
                        if prior_value is None:
                            os.environ.pop(key, None)
                        else:
                            os.environ[key] = prior_value

                    try:
                        if sandbox_id is not None:
                            backend.revoke_gateway(
                                sandbox_id,
                                port=gateway_port,
                                gateway_url=mcp_gateway_agent_url,
                            )
                    finally:
                        try:
                            if (
                                sandbox_id is not None
                                and not self.config.keep_container
                            ):
                                backend.remove(sandbox_id)
                        finally:
                            try:
                                collect_artifacts(workspace)
                            finally:
                                # Keep the manifest even when artifact collection or
                                # earlier sandbox cleanup fails.
                                (session_dir / MANIFEST_FILENAME).write_text(
                                    json.dumps(manifest, indent=2),
                                    encoding="utf-8",
                                )
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
                    _sandbox_env_lock.release()
        finally:
            backend.release_host()

    def _run_sdk_step(
        self,
        *,
        sandbox_session: SandboxSession,
        phase: str,
        stream_output: bool,
        timeout_sec: float | None,
        budget_sec: int,
    ) -> None:
        """Run one pipeline phase of the SDK agent inside the sandbox."""
        workspace = self.backend.workspace_path(sandbox_session.workspace_dir)
        inner = (
            f"cd {shlex.quote(workspace)} && "
            f"PYTHONPATH={shlex.quote(workspace + '/agent')} "
            f"python3 -m agent.sandbox.runner {phase}"
        )
        proc = self.backend.exec_stream(sandbox_session.sandbox_name, inner)
        assert proc.stdout is not None
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            proc.kill()

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
            if timer is not None:
                timer.cancel()
        if timed_out.is_set():
            from agent.registry import AgentTimeoutError

            raise AgentTimeoutError(
                f"agent run exceeded agent.timeout_sec ({budget_sec}s)"
            )
        if returncode != 0:
            detail = "".join(captured).strip() or "(no output)"
            raise RuntimeError(
                f"SDK sandbox runner ({phase}) exited with code {returncode}:\n{detail}"
            )

    def _run_sdk_in_sandbox(
        self,
        *,
        sandbox_session: SandboxSession,
        stream_output: bool,
        timeout_sec: int,
    ) -> None:
        """Diagnosis in the sandbox, freeze + phase advance on the host, then submission.

        The gateway's phase-advance secret stays in this host process, so the
        sandboxed agent cannot reach the submission context before its
        diagnosis step has exited.
        """
        from agent.utils.mcp_client import begin_submission_mcp_phase

        deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None

        def remaining() -> float | None:
            return None if deadline is None else deadline - time.monotonic()

        sandbox_id = sandbox_session.sandbox_name
        workspace = sandbox_session.workspace_dir
        mirror = sandbox_session.trace_mirror
        self._run_sdk_step(
            sandbox_session=sandbox_session,
            phase=DIAGNOSIS,
            stream_output=stream_output,
            timeout_sec=remaining(),
            budget_sec=timeout_sec,
        )
        self.backend.pull_files(sandbox_id, workspace, [DIAGNOSIS_REPORT_FILENAME])
        report_path = workspace / DIAGNOSIS_REPORT_FILENAME
        try:
            report = json.loads(read_workspace_text(report_path))["report"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"SDK diagnosis step left no report: {exc}") from exc
        if not isinstance(report, str):
            raise RuntimeError("SDK diagnosis step left a non-text report")
        if mirror is not None:
            mirror.set_phase(SUBMISSION)
        context = begin_submission_mcp_phase(sandbox_session.agent_session_id, report)
        write_workspace_text(
            workspace / SUBMISSION_CONTEXT_FILENAME,
            json.dumps(context, ensure_ascii=False),
        )
        self.backend.push_files(sandbox_id, workspace, [SUBMISSION_CONTEXT_FILENAME])
        self._run_sdk_step(
            sandbox_session=sandbox_session,
            phase=SUBMISSION,
            stream_output=stream_output,
            timeout_sec=remaining(),
            budget_sec=timeout_sec,
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
            if agent_type in SDK_AGENT_TYPES:
                self._run_sdk_in_sandbox(
                    sandbox_session=sandbox_session,
                    stream_output=stream_output,
                    timeout_sec=timeout_sec,
                )
            else:
                from agent.registry import create_agent, run_agent

                agent = create_agent(
                    agent_type,
                    session_id=resolve_agent_session_id(session),
                    llm_provider=llm_provider,
                    model=model,
                    max_steps=max_steps,
                    reasoning_effort=reasoning_effort,
                    max_tokens=max_tokens,
                    stream_output=stream_output,
                )
                run_agent(agent, session.task_description, timeout_sec=timeout_sec)

        return SandboxRunResult(returncode=0, sandbox_name=sandbox_session.sandbox_name)
