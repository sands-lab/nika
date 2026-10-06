"""Run troubleshooting agents using native Docker Sandboxes agents."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from agent.sandbox.config import ENV_SESSION_DIR, SandboxConfig
from agent.sandbox.constants import (
    DIAGNOSIS_REPORT_FILENAME,
    MANIFEST_FILENAME,
    SUBMISSION_CONTEXT_FILENAME,
)
from agent.sandbox.env import format_env_for_log
from agent.sandbox.mcp_manifest import build_sandbox_mcp_servers
from agent.protocols import DIAGNOSIS, SUBMISSION
from agent.sandbox.redact import redact_text
from agent.sandbox.sbx.agents import ENV_SBX_SANDBOX_NAME, native_sbx_agent
from agent.sandbox.sbx.client import (
    ensure_sbx_ready,
    require_sbx_authenticated,
    run_sbx_checked,
    run_sbx_optional,
    sbx_host_lock,
    stream_sbx,
)
from agent.sandbox.sbx.credentials import (
    ensure_sbx_credentials,
    required_services_for_agent,
)
from agent.sandbox.sbx.policy import (
    allow_mcp_gateway,
    deny_mcp_gateway,
    ensure_llm_network_policy,
    ensure_pypi_network_policy,
    sanitize_sandbox_name,
)
from agent.sandbox.sbx.proxy import ensure_sbx_proxy_config, resolve_sbx_upstream_proxy
from agent.utils.skills import ENV_ENABLE_SKILLS, skills_enabled
from agent.sandbox.sbx.wheels import (
    install_sdk_packages_in_sandbox,
    stage_sdk_wheels,
)
from agent.sandbox.sbx.workspace import (
    TraceMirror,
    cleanup_workspace,
    collect_artifacts,
    prepare_workspace,
    trace_mirror,
)
from nika.utils.agent_session_id import resolve_agent_session_id
from nika.utils.logger import elapsed_ms, log_event
from nika.utils.session import Session

logger = logging.getLogger(__name__)

SDK_AGENT_TYPES = frozenset({"sdk.codex_sdk", "sdk.claude_sdk", "community.sade"})
# Covers a first-run template image pull; a normal create takes under a minute.
_SBX_CREATE_TIMEOUT_SEC = 600

# Host-side CLI sandboxes read ``NIKA_SBX_SANDBOX_NAME`` / ``NIKA_SESSION_DIR``
# from process env. Concurrent sessions in one process must not interleave
# save/restore of those keys (benchmark parallel batches use spawn workers;
# this lock still protects in-process callers and nested contexts).
_sandbox_env_lock = threading.RLock()


@dataclass
class SbxSandboxRunResult:
    returncode: int
    sandbox_name: str


@dataclass
class SbxSession:
    sandbox_name: str
    workspace_dir: Path
    gateway_port: int
    agent_session_id: str = ""
    # In-VM SDK agents only: copies the workspace trace into the host trace.
    trace_mirror: TraceMirror | None = None


class SbxSandboxManager:
    """Create native sbx sandboxes and run NIKA agents on the host."""

    def __init__(self, config: SandboxConfig) -> None:
        self.config = config

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

    def build_create_command(
        self,
        *,
        sandbox_name: str,
        sbx_agent: str,
        workspace_dir: Path,
        agent_type: str,
    ) -> list[str]:
        cmd = [
            "create",
            "--name",
            sandbox_name,
            sbx_agent,
            str(workspace_dir),
        ]
        # SDK agents install deps after create (offline wheels when enabled;
        # otherwise PyPI via sbx exec). Avoid `--kit` install hooks: slow PyPI
        # downloads inside the microVM are frequently SIGKILL'd (exit 137),
        # surfacing as sbx HTTP 500.
        if self.config.cpus:
            cmd.extend(["--cpus", str(self.config.cpus)])
        if self.config.memory:
            cmd.extend(["-m", self.config.memory])
        return cmd

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
    ) -> Iterator[SbxSession]:
        agent_sid = resolve_agent_session_id(session)
        sandbox_name = sanitize_sandbox_name(agent_sid)
        sbx_agent = native_sbx_agent(agent_type)
        session_dir = Path(session.session_dir).resolve()

        upstream_proxy = resolve_sbx_upstream_proxy(env_file=self.config.env_file)
        ensure_sbx_proxy_config(upstream_proxy)
        require_sbx_authenticated()
        ensure_sbx_ready()

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

        # Workspace path is chosen before prepare so PYTHONPATH matches the mount.
        from agent.sandbox.sbx.workspace import opaque_agent_workspace_dir

        workspace_path = opaque_agent_workspace_dir(agent_sid)

        runtime_env = {
            "NIKA_SANDBOX_EXECUTION": "1",
            "NIKA_SESSION_ID": agent_sid,
            "NIKA_MCP_GATEWAY_AGENT_URL": mcp_gateway_agent_url.rstrip("/"),
            "PYTHONPATH": str(workspace_path / "agent"),
            ENV_ENABLE_SKILLS: "1" if skills_enabled() else "0",
        }
        backend = getattr(session, "backend", "").strip()
        if backend:
            runtime_env["NIKA_SESSION_BACKEND"] = backend

        cred_plan = ensure_sbx_credentials(
            env_file=self.config.env_file,
            required_services=required_services_for_agent(agent_type),
            provider=llm_provider,
            agent_type=agent_type,
        )
        # Allow custom / self-hosted LLM hosts in addition to the stock list.
        from agent.sandbox.sbx.policy import llm_network_resources_for_url

        extra_hosts: list[str] = []
        for url in (cred_plan.openai_base_url, cred_plan.anthropic_base_url):
            extra_hosts.extend(llm_network_resources_for_url(url))
        ensure_llm_network_policy(*extra_hosts)
        runtime_env.update(cred_plan.sentinel_runtime_env())
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
            if self.config.offline_sdk_wheels:
                stage_sdk_wheels(workspace.workspace_dir)
            mirror = trace_mirror(workspace)

        run_sbx_optional(["rm", "--force", sandbox_name])

        create_cmd = self.build_create_command(
            sandbox_name=sandbox_name,
            sbx_agent=sbx_agent,
            workspace_dir=workspace.workspace_dir,
            agent_type=agent_type,
        )
        # Lifetime covers create → agent run → teardown; setup span is logged
        # on sandbox_start once the sandbox is ready (env_start-style).
        lifetime_started = time.perf_counter()
        setup_started = lifetime_started

        # Serialize host env mutation for the lifetime of this sandbox session so
        # a sibling session cannot restore/clobber our NIKA_SBX_* values mid-run.
        _sandbox_env_lock.acquire()
        prior_session_dir = os.environ.get(ENV_SESSION_DIR)
        prior_sbx_name = os.environ.get(ENV_SBX_SANDBOX_NAME)
        prior_runtime_env = {key: os.environ.get(key) for key in runtime_env}
        try:
            # sbx fails a microVM that does not connect within a fixed 15s, and
            # concurrent boots on a loaded host routinely exceed that. The
            # timeout bounds how long a hung create can stall sibling trials.
            with sbx_host_lock("create"):
                run_sbx_checked(create_cmd, timeout=_SBX_CREATE_TIMEOUT_SEC)
            if agent_type in SDK_AGENT_TYPES:
                if not self.config.offline_sdk_wheels:
                    ensure_pypi_network_policy()
                install_sdk_packages_in_sandbox(
                    sandbox_name=sandbox_name,
                    workspace_dir=workspace.workspace_dir,
                    offline=self.config.offline_sdk_wheels,
                )
            allow_mcp_gateway(
                sandbox_name=sandbox_name,
                port=gateway_port,
                gateway_url=mcp_gateway_agent_url,
            )
            log_event(
                "sandbox_start",
                f"Created native Docker Sandbox ({sbx_agent}) for session {session.session_id}",
                session_id=session.session_id,
                agent_session_id=agent_sid,
                agent_type=agent_type,
                sandbox_name=sandbox_name,
                native_sbx_agent=sbx_agent,
                mcp_gateway=mcp_gateway_agent_url,
                upstream_proxy=upstream_proxy,
                offline_sdk_wheels=self.config.offline_sdk_wheels,
                sbx_command=redact_text("sbx " + " ".join(create_cmd)),
                env=format_env_for_log(runtime_env),
                duration_ms=elapsed_ms(setup_started),
            )
            os.environ[ENV_SBX_SANDBOX_NAME] = sandbox_name
            os.environ[ENV_SESSION_DIR] = str(workspace.workspace_dir)
            # Force credential placeholders over host .env API keys so the
            # microVM never receives real secrets (and DeepSeek set-custom
            # placeholders are not shadowed by host dotenv values).
            _force_env_keys = {
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "DEEPSEEK_API_KEY",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_BASE_URL",
            }
            for key, value in runtime_env.items():
                if key in _force_env_keys:
                    os.environ[key] = value
                else:
                    os.environ.setdefault(key, value)
            if mirror is not None:
                mirror.start()
            yield SbxSession(
                sandbox_name=sandbox_name,
                workspace_dir=workspace.workspace_dir,
                gateway_port=gateway_port,
                agent_session_id=agent_sid,
                trace_mirror=mirror,
            )
        finally:
            try:
                if mirror is not None:
                    mirror.stop()
                if prior_sbx_name is None:
                    os.environ.pop(ENV_SBX_SANDBOX_NAME, None)
                else:
                    os.environ[ENV_SBX_SANDBOX_NAME] = prior_sbx_name
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
                    deny_mcp_gateway(
                        sandbox_name=sandbox_name,
                        port=gateway_port,
                        gateway_url=mcp_gateway_agent_url,
                    )
                finally:
                    try:
                        if not self.config.keep_container:
                            run_sbx_optional(["rm", "--force", sandbox_name])
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
                                f"Docker Sandbox finished for session {session.session_id}",
                                session_id=session.session_id,
                                agent_type=agent_type,
                                sandbox_name=sandbox_name,
                                duration_ms=elapsed_ms(lifetime_started),
                            )
            finally:
                _sandbox_env_lock.release()

    def _run_sdk_step(
        self,
        *,
        sbx_session: SbxSession,
        phase: str,
        stream_output: bool,
        timeout_sec: float | None,
        budget_sec: int,
    ) -> None:
        """Run one pipeline phase of the SDK agent inside the sandbox."""
        workspace = sbx_session.workspace_dir
        py_path = workspace / "agent"
        inner = (
            f"cd {workspace} && PYTHONPATH={py_path} "
            f"python3 -m agent.sandbox.runner {phase}"
        )
        proc = stream_sbx(["exec", sbx_session.sandbox_name, "bash", "-lc", inner])
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
                    import sys

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
        sbx_session: SbxSession,
        stream_output: bool,
        timeout_sec: int,
    ) -> None:
        """Diagnosis in the VM, freeze + phase advance on the host, then submission.

        The gateway's phase-advance secret stays in this host process, so the
        in-VM agent cannot reach the submission context before its diagnosis
        step has exited.
        """
        from agent.utils.mcp_client import begin_submission_mcp_phase

        deadline = time.monotonic() + timeout_sec if timeout_sec > 0 else None

        def remaining() -> float | None:
            return None if deadline is None else deadline - time.monotonic()

        workspace = sbx_session.workspace_dir
        mirror = sbx_session.trace_mirror
        self._run_sdk_step(
            sbx_session=sbx_session,
            phase=DIAGNOSIS,
            stream_output=stream_output,
            timeout_sec=remaining(),
            budget_sec=timeout_sec,
        )
        report_path = workspace / DIAGNOSIS_REPORT_FILENAME
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))["report"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(f"SDK diagnosis step left no report: {exc}") from exc
        if not isinstance(report, str):
            raise RuntimeError("SDK diagnosis step left a non-text report")
        if mirror is not None:
            mirror.set_phase(SUBMISSION)
        context = begin_submission_mcp_phase(sbx_session.agent_session_id, report)
        (workspace / SUBMISSION_CONTEXT_FILENAME).write_text(
            json.dumps(context, ensure_ascii=False), encoding="utf-8"
        )
        self._run_sdk_step(
            sbx_session=sbx_session,
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
    ) -> SbxSandboxRunResult:
        """Create a native sandbox, run the agent, collect artifacts."""
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
        ) as sbx_session:
            if agent_type in SDK_AGENT_TYPES:
                self._run_sdk_in_sandbox(
                    sbx_session=sbx_session,
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

        return SbxSandboxRunResult(
            returncode=0,
            sandbox_name=sanitize_sandbox_name(resolve_agent_session_id(session)),
        )
