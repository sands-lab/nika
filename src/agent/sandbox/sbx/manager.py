"""Run troubleshooting agents using native Docker Sandboxes agents."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from agent.sandbox.config import ENV_SESSION_DIR, SandboxConfig
from agent.sandbox.constants import MANIFEST_FILENAME
from agent.sandbox.env import format_env_for_log
from agent.sandbox.redact import redact_text
from agent.sandbox.sbx.agents import ENV_SBX_SANDBOX_NAME, native_sbx_agent
from agent.sandbox.sbx.client import (
    ensure_sbx_ready,
    run_sbx_checked,
    run_sbx_optional,
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
from agent.sandbox.sbx.wheels import (
    install_sdk_packages_in_sandbox,
    stage_sdk_wheels,
)
from agent.sandbox.sbx.workspace import (
    SKILLS_DIRNAME,
    cleanup_workspace,
    collect_artifacts,
    prepare_workspace,
)
from agent.sandbox.mcp_manifest import build_sandbox_mcp_servers
from nika.utils.logger import log_event
from nika.utils.session import Session

logger = logging.getLogger(__name__)

SDK_AGENT_TYPES = frozenset({"sdk.codex_sdk", "sdk.claude_sdk", "community.sade"})


@dataclass
class SbxSandboxRunResult:
    returncode: int
    sandbox_name: str


@dataclass
class SbxSession:
    sandbox_name: str
    workspace_dir: Path
    gateway_port: int


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
        llm_provider: str | None,
        mcp_gateway_agent_url: str,
        stream_output: bool,
    ) -> dict:
        gateway_url = mcp_gateway_agent_url.rstrip("/")
        scenario_name = getattr(session, "scenario_name", "")
        backend = getattr(session, "backend", "") or "kathara"
        manifest = {
            "session_id": session.session_id,
            "session_dir": str(Path(session.session_dir).resolve()),
            "agent_type": agent_type,
            "model": model,
            "max_steps": max_steps,
            "reasoning_effort": reasoning_effort,
            "llm_provider": llm_provider,
            "task_description": session.task_description,
            "scenario_name": scenario_name,
            "backend": backend,
            "mcp_gateway_agent_url": gateway_url,
            "stream_output": stream_output,
        }
        if agent_type in SDK_AGENT_TYPES:
            manifest["mcp_servers"] = build_sandbox_mcp_servers(
                session_id=session.session_id,
                scenario_name=scenario_name,
                backend=backend,
                gateway_agent_url=gateway_url,
            )
        return manifest

    def _bundle_agent_sources(self, workspace_dir: Path) -> None:
        """Copy agent code (prompts, SDK workers) into the sandbox workspace."""
        from nika.config import _REPO_ROOT

        src = _REPO_ROOT / "src" / "agent"
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
            cmd.extend(["--cpus", self.config.cpus])
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
        llm_provider: str | None,
        mcp_gateway_agent_url: str,
        gateway_port: int,
        stream_output: bool,
    ) -> Iterator[SbxSession]:
        sandbox_name = sanitize_sandbox_name(session.session_id)
        sbx_agent = native_sbx_agent(agent_type)
        session_dir = Path(session.session_dir).resolve()
        workspace_path = session_dir / ".sandbox_run"
        workspace_skills = workspace_path / SKILLS_DIRNAME

        upstream_proxy = resolve_sbx_upstream_proxy(env_file=self.config.env_file)
        ensure_sbx_proxy_config(upstream_proxy)
        ensure_sbx_ready()
        ensure_llm_network_policy()

        manifest = self.write_manifest(
            session=session,
            agent_type=agent_type,
            model=model,
            max_steps=max_steps,
            reasoning_effort=reasoning_effort,
            llm_provider=llm_provider,
            mcp_gateway_agent_url=mcp_gateway_agent_url,
            stream_output=stream_output,
        )

        runtime_env = {
            "NIKA_SANDBOX_EXECUTION": "1",
            "NIKA_SESSION_ID": session.session_id,
            "NIKA_AGENT_TYPE": agent_type,
            "NIKA_MODEL": model,
            "NIKA_MCP_GATEWAY_AGENT_URL": mcp_gateway_agent_url.rstrip("/"),
            "NIKA_SKILLS_DIR": str(workspace_skills),
            "PYTHONPATH": str(workspace_path / "agent"),
        }
        backend = getattr(session, "backend", "").strip()
        if backend:
            runtime_env["NIKA_SESSION_BACKEND"] = backend

        cred_plan = ensure_sbx_credentials(
            env_file=self.config.env_file,
            required_services=required_services_for_agent(agent_type),
        )
        runtime_env.update(cred_plan.sentinel_runtime_env())
        workspace = prepare_workspace(
            session_dir=session_dir,
            manifest=manifest,
            runtime_env=runtime_env,
        )
        if agent_type in SDK_AGENT_TYPES:
            self._bundle_agent_sources(workspace.workspace_dir)
            if self.config.offline_sdk_wheels:
                stage_sdk_wheels(workspace.workspace_dir)

        run_sbx_optional(["rm", "--force", sandbox_name])

        create_cmd = self.build_create_command(
            sandbox_name=sandbox_name,
            sbx_agent=sbx_agent,
            workspace_dir=workspace.workspace_dir,
            agent_type=agent_type,
        )
        log_event(
            "sandbox_start",
            f"Creating native Docker Sandbox ({sbx_agent}) for session {session.session_id}",
            session_id=session.session_id,
            agent_type=agent_type,
            sandbox_name=sandbox_name,
            native_sbx_agent=sbx_agent,
            mcp_gateway=mcp_gateway_agent_url,
            upstream_proxy=upstream_proxy,
            offline_sdk_wheels=self.config.offline_sdk_wheels,
            sbx_command=redact_text("sbx " + " ".join(create_cmd)),
            env=format_env_for_log(runtime_env),
        )

        prior_session_dir = os.environ.get(ENV_SESSION_DIR)
        prior_sbx_name = os.environ.get(ENV_SBX_SANDBOX_NAME)
        prior_runtime_env = {key: os.environ.get(key) for key in runtime_env}
        try:
            run_sbx_checked(create_cmd)
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
            os.environ[ENV_SBX_SANDBOX_NAME] = sandbox_name
            os.environ[ENV_SESSION_DIR] = str(workspace.workspace_dir)
            # Force credential placeholders over host .env API keys so the
            # microVM never receives real secrets (and DeepSeek set-custom
            # placeholders are not shadowed by host dotenv values).
            _force_env_keys = {
                "OPENAI_API_KEY",
                "DEEPSEEK_API_KEY",
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_BASE_URL",
            }
            for key, value in runtime_env.items():
                if key in _force_env_keys:
                    os.environ[key] = value
                else:
                    os.environ.setdefault(key, value)
            yield SbxSession(
                sandbox_name=sandbox_name,
                workspace_dir=workspace.workspace_dir,
                gateway_port=gateway_port,
            )
        finally:
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

            deny_mcp_gateway(
                sandbox_name=sandbox_name,
                port=gateway_port,
                gateway_url=mcp_gateway_agent_url,
            )
            if not self.config.keep_container:
                run_sbx_optional(["rm", "--force", sandbox_name])
            collect_artifacts(workspace)
            (session_dir / MANIFEST_FILENAME).write_text(
                __import__("json").dumps(manifest, indent=2),
                encoding="utf-8",
            )
            cleanup_workspace(workspace)
            log_event(
                "sandbox_end",
                f"Docker Sandbox finished for session {session.session_id}",
                session_id=session.session_id,
                agent_type=agent_type,
                sandbox_name=sandbox_name,
            )

    def _run_sdk_in_sandbox(
        self,
        *,
        sbx_session: SbxSession,
        stream_output: bool,
    ) -> None:
        workspace = sbx_session.workspace_dir
        py_path = workspace / "agent"
        inner = (
            f"cd {workspace} && PYTHONPATH={py_path} python3 -m agent.sandbox.runner"
        )
        proc = stream_sbx(
            ["exec", "-d", sbx_session.sandbox_name, "bash", "-lc", inner]
        )
        assert proc.stdout is not None
        captured: list[str] = []
        for line in proc.stdout:
            captured.append(line)
            if stream_output:
                import sys

                sys.stdout.write(line)
                sys.stdout.flush()
        returncode = proc.wait()
        if returncode != 0:
            detail = "".join(captured).strip() or "(no output)"
            raise RuntimeError(
                f"SDK sandbox runner exited with code {returncode}:\n{detail}"
            )

    def run(
        self,
        *,
        session: Session,
        agent_type: str,
        model: str,
        max_steps: int | None,
        reasoning_effort: str | None,
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
            llm_provider=llm_provider,
            mcp_gateway_agent_url=mcp_gateway_agent_url,
            gateway_port=gateway_port,
            stream_output=stream_output,
        ) as sbx_session:
            if agent_type in SDK_AGENT_TYPES:
                self._run_sdk_in_sandbox(
                    sbx_session=sbx_session,
                    stream_output=stream_output,
                )
            else:
                from agent.registry import create_agent

                agent = create_agent(
                    agent_type,
                    session_id=session.session_id,
                    llm_provider=llm_provider,
                    model=model,
                    max_steps=max_steps,
                    reasoning_effort=reasoning_effort,
                    stream_output=stream_output,
                )
                asyncio.run(agent.run(task_description=session.task_description))

        return SbxSandboxRunResult(
            returncode=0, sandbox_name=sanitize_sandbox_name(session.session_id)
        )
