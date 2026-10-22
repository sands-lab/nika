"""Docker Sandboxes (sbx) backend: native agent templates in local microVMs."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from nika.agents.registry import get_agent_spec
from nika.agents.sandbox.backend import (
    SandboxBackend,
    SandboxCleanupResult,
    StreamedProcess,
)
from nika.agents.sandbox.config import SANDBOX_GATEWAY_HOST_BRIDGE
from nika.agents.sandbox.model_calls import ModelCallLog
from nika.agents.sandbox.model_proxy import start_model_proxy
from nika.agents.sandbox.redact import redact_text
from nika.agents.sandbox.sbx.agents import (
    native_sbx_agent,
    required_sbx_template_images,
    sbx_template_image,
)
from nika.agents.sandbox.sbx.client import (
    ensure_sbx_ready,
    require_sbx_authenticated,
    run_sbx_checked,
    run_sbx_optional,
    sbx_available,
    sbx_host_lock,
    stream_sbx,
)
from nika.agents.sandbox.sbx.policy import (
    allow_mcp_gateway,
    allow_network_hosts,
    deny_mcp_gateway,
    deny_network_resources,
    ensure_pypi_network_policy,
    revoke_pypi_network_policy,
    sandbox_network_lockdown,
    sanitize_sandbox_name,
)
from nika.agents.sandbox.sbx.proxy import (
    ensure_sbx_proxy_config,
    resolve_sbx_upstream_proxy,
)
from nika.agents.sandbox.sbx.wheels import install_agent_packages_in_sandbox
from nika.agents.sandbox.wheels import stage_wheels

logger = logging.getLogger(__name__)

# Covers a first-run template image pull; a normal create takes under a minute.
_SBX_CREATE_TIMEOUT_SEC = 600


class SbxBackend(SandboxBackend):
    """Docker Sandboxes microVMs that bind-mount the workspace at its host path."""

    name = "sbx"
    gateway_agent_host = SANDBOX_GATEWAY_HOST_BRIDGE

    def __init__(self, config) -> None:
        super().__init__(config)
        self._upstream_proxy: str | None = None
        self._native_agent = ""
        self._create_command: list[str] = []
        self._host_stack = ExitStack()
        self._model_proxy_port: int | None = None

    def ensure_available(self) -> None:
        if not sbx_available():
            raise RuntimeError(
                "Docker Sandboxes CLI (sbx) is not available. "
                "Install docker-sbx and run `sbx login`."
            )

    def prepare_templates(self, agent_types: Sequence[str]) -> list[str]:
        """Return the sbx template images for *agent_types* to pull on the host."""
        return required_sbx_template_images(*agent_types)

    def prepare_host(
        self,
        *,
        agent_type: str,
        llm_provider: str | None,
        call_log: ModelCallLog | None = None,
    ) -> dict[str, str]:
        if not llm_provider:
            raise RuntimeError("Set agent.provider for sandboxed agents.")
        self._upstream_proxy = resolve_sbx_upstream_proxy(env_file=self.config.env_file)
        ensure_sbx_proxy_config(self._upstream_proxy)
        require_sbx_authenticated()
        ensure_sbx_ready()
        # The proxy holds the key (or subscription login), adapts requests for
        # custom endpoints, and records every call. The microVM reaches it on
        # the host bridge, and the per-session token keeps other clients out.
        self._model_proxy_port, env = start_model_proxy(
            self._host_stack,
            env_file=self.config.env_file,
            agent_type=agent_type,
            provider=llm_provider,
            bind_host="0.0.0.0",
            agent_host=SANDBOX_GATEWAY_HOST_BRIDGE,
            call_log=call_log,
        )
        return env

    def stage_workspace(self, workspace_dir: Path, *, agent_type: str) -> None:
        if self.config.offline_sdk_wheels:
            stage_wheels(workspace_dir, get_agent_spec(agent_type).requirements)

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
        # Agent packages are installed after create (offline wheels when
        # enabled; otherwise PyPI via sbx exec). Avoid `--kit` install hooks: slow PyPI
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
            spec = get_agent_spec(agent_type)
            use_pypi = bool(spec.packages) or not self.config.offline_sdk_wheels
            # The sbx policy is shared with the user's other sandboxes, so the
            # global allow rules stay; this sandbox denies each of them and
            # reaches only the MCP gateway, the model proxy, and an added
            # agent's declared egress. Package indexes stay open until the
            # install finishes.
            proxy = (
                (f"localhost:{self._model_proxy_port}",)
                if self._model_proxy_port is not None
                else ()
            )
            rest, install = sandbox_network_lockdown(
                sandbox_name,
                keep_hosts=spec.egress,
                keep_resources=proxy,
                install_pypi=use_pypi,
            )
            allow_network_hosts(sandbox_name, *spec.egress)
            if use_pypi:
                ensure_pypi_network_policy(sandbox_name)
            with ThreadPoolExecutor(max_workers=1) as pool:
                denied = pool.submit(deny_network_resources, sandbox_name, rest)
                install_agent_packages_in_sandbox(
                    sandbox_name=sandbox_name,
                    workspace_dir=workspace_dir,
                    requirements=spec.requirements,
                    packages=spec.packages,
                    offline=self.config.offline_sdk_wheels,
                )
                denied.result()
            if use_pypi:
                revoke_pypi_network_policy(sandbox_name)
            deny_network_resources(sandbox_name, install)
            logger.info(
                "Denied %d network resources in sbx sandbox %s",
                len(rest) + len(install),
                sandbox_name,
            )
        except BaseException:
            if not self.config.keep_container:
                run_sbx_optional(["rm", "--force", sandbox_name])
            raise
        return sandbox_name

    def allow_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        allow_mcp_gateway(sandbox_name=sandbox_id, port=port, gateway_url=gateway_url)
        if self._model_proxy_port is not None:
            allow_mcp_gateway(sandbox_name=sandbox_id, port=self._model_proxy_port)

    def revoke_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        deny_mcp_gateway(sandbox_name=sandbox_id, port=port, gateway_url=gateway_url)
        if self._model_proxy_port is not None:
            deny_mcp_gateway(sandbox_name=sandbox_id, port=self._model_proxy_port)

    def exec_stream(self, sandbox_id: str, command: str) -> StreamedProcess:
        return stream_sbx(["exec", sandbox_id, "bash", "-lc", command])

    def remove(self, sandbox_id: str) -> None:
        run_sbx_optional(["rm", "--force", sandbox_id])

    def release_host(self) -> None:
        self._host_stack.close()
        self._model_proxy_port = None

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
        from nika.agents.sandbox.sbx.cleanup import cleanup_sbx_for_session

        return cleanup_sbx_for_session(session_meta)
