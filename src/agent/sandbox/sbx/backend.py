"""Docker Sandboxes (sbx) backend: native agent templates in local microVMs."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agent.sandbox.backend import (
    SandboxBackend,
    SandboxCleanupResult,
    StreamedProcess,
)
from agent.sandbox.constants import SDK_AGENT_TYPES
from agent.sandbox.redact import redact_text
from agent.sandbox.sbx.agents import native_sbx_agent, sbx_template_image
from agent.sandbox.sbx.client import (
    ensure_sbx_ready,
    require_sbx_authenticated,
    run_sbx_checked,
    run_sbx_optional,
    sbx_available,
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
    llm_network_resources_for_url,
    sanitize_sandbox_name,
)
from agent.sandbox.sbx.proxy import ensure_sbx_proxy_config, resolve_sbx_upstream_proxy
from agent.sandbox.sbx.wheels import (
    install_sdk_packages_in_sandbox,
    stage_sdk_wheels,
)

# Covers a first-run template image pull; a normal create takes under a minute.
_SBX_CREATE_TIMEOUT_SEC = 600


class SbxBackend(SandboxBackend):
    """Docker Sandboxes microVMs that bind-mount the workspace at its host path."""

    name = "sbx"

    def __init__(self, config) -> None:
        super().__init__(config)
        self._upstream_proxy: str | None = None
        self._native_agent = ""
        self._create_command: list[str] = []

    def ensure_available(self) -> None:
        if not sbx_available():
            raise RuntimeError(
                "Docker Sandboxes CLI (sbx) is not available. "
                "Install docker-sbx and run `sbx login`."
            )

    def prepare_host(
        self, *, agent_type: str, llm_provider: str | None
    ) -> dict[str, str]:
        self._upstream_proxy = resolve_sbx_upstream_proxy(env_file=self.config.env_file)
        ensure_sbx_proxy_config(self._upstream_proxy)
        require_sbx_authenticated()
        ensure_sbx_ready()

        cred_plan = ensure_sbx_credentials(
            env_file=self.config.env_file,
            required_services=required_services_for_agent(agent_type),
            provider=llm_provider,
            agent_type=agent_type,
        )
        # Allow custom / self-hosted LLM hosts in addition to the stock list.
        extra_hosts: list[str] = []
        for url in (cred_plan.openai_base_url, cred_plan.anthropic_base_url):
            extra_hosts.extend(llm_network_resources_for_url(url))
        ensure_llm_network_policy(*extra_hosts)
        return cred_plan.sentinel_runtime_env()

    def stage_workspace(self, workspace_dir: Path, *, agent_type: str) -> None:
        if agent_type in SDK_AGENT_TYPES and self.config.offline_sdk_wheels:
            stage_sdk_wheels(workspace_dir)

    def build_create_command(
        self,
        *,
        sandbox_name: str,
        sbx_agent: str,
        workspace_dir: Path,
    ) -> list[str]:
        cmd = [
            "create",
            "--name",
            sandbox_name,
            "--template",
            sbx_template_image(sbx_agent),
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

    def create(
        self, *, agent_session_id: str, agent_type: str, workspace_dir: Path
    ) -> str:
        sandbox_name = sanitize_sandbox_name(agent_session_id)
        self._native_agent = native_sbx_agent(agent_type)
        self._create_command = self.build_create_command(
            sandbox_name=sandbox_name,
            sbx_agent=self._native_agent,
            workspace_dir=workspace_dir,
        )
        run_sbx_optional(["rm", "--force", sandbox_name])
        try:
            # sbx fails a microVM that does not connect within a fixed 15s, and
            # concurrent boots on a loaded host routinely exceed that. The
            # timeout bounds how long a hung create can stall sibling trials.
            with sbx_host_lock("create"):
                run_sbx_checked(self._create_command, timeout=_SBX_CREATE_TIMEOUT_SEC)
            if agent_type in SDK_AGENT_TYPES:
                if not self.config.offline_sdk_wheels:
                    ensure_pypi_network_policy()
                install_sdk_packages_in_sandbox(
                    sandbox_name=sandbox_name,
                    workspace_dir=workspace_dir,
                    offline=self.config.offline_sdk_wheels,
                )
        except BaseException:
            if not self.config.keep_container:
                run_sbx_optional(["rm", "--force", sandbox_name])
            raise
        return sandbox_name

    def allow_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        allow_mcp_gateway(sandbox_name=sandbox_id, port=port, gateway_url=gateway_url)

    def revoke_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        deny_mcp_gateway(sandbox_name=sandbox_id, port=port, gateway_url=gateway_url)

    def exec_stream(self, sandbox_id: str, command: str) -> StreamedProcess:
        return stream_sbx(["exec", sandbox_id, "bash", "-lc", command])

    def remove(self, sandbox_id: str) -> None:
        run_sbx_optional(["rm", "--force", sandbox_id])

    def start_log_fields(self) -> dict[str, Any]:
        return {
            "native_sbx_agent": self._native_agent,
            "upstream_proxy": self._upstream_proxy,
            "offline_sdk_wheels": self.config.offline_sdk_wheels,
            "sbx_command": redact_text("sbx " + " ".join(self._create_command)),
        }

    @classmethod
    def cleanup_session(
        cls, session_meta: Mapping[str, Any]
    ) -> SandboxCleanupResult | None:
        from agent.sandbox.sbx.cleanup import cleanup_sbx_for_session

        return cleanup_sbx_for_session(session_meta)
