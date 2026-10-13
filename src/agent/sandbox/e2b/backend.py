"""E2B-compatible backend: sandboxes on E2B or a self-hosted service (AgentENV).

These sandboxes have no route to the NIKA host (AgentENV denies private
ranges at node level) and no internet access (created with
``allow_internet_access=False``). The agent reaches its MCP gateway and the
host model proxy through the tunnel in :mod:`agent.sandbox.tunnel`, which runs
over the same command channel NIKA uses to drive the sandbox. The model proxy
adds the real provider key on the host, so the sandbox only holds a
per-session token.
"""

from __future__ import annotations

import logging
import os
import platform
import queue
import re
import shutil
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent.sandbox.backend import (
    SandboxBackend,
    SandboxCleanupResult,
    StreamedProcess,
)
from agent.sandbox.config import (
    SandboxConfig,
    load_credential_sources,
    load_sandbox_env_values,
)
from agent.sandbox.constants import SDK_AGENT_TYPES
from agent.sandbox.locks import host_file_lock
from agent.sandbox.templates import AGENT_TEMPLATES, TEMPLATE_IMAGES
from agent.sandbox.tunnel import RELAY_SCRIPT, TunnelBridge
from agent.sandbox.wheels import (
    sdk_pip_install_command,
    stage_sdk_requirements,
    stage_sdk_wheels,
)
from agent.sandbox.workspace import write_workspace_bytes
from nika.utils.logger import elapsed_ms, log_event

logger = logging.getLogger(__name__)

ENV_E2B_API_KEY = "E2B_API_KEY"
# Where the host workspace is uploaded inside the sandbox.
REMOTE_WORKSPACE = "/tmp/nika-workspace"
_RELAY_NAME = "nika_tunnel_relay.py"
_METADATA_SESSION_KEY = "nika_agent_session_id"
_ANTHROPIC_API_URL = "https://api.anthropic.com"
# Agents this backend runs today. CLI agents need host-side exec through the
# backend, and Codex needs an OpenAI-protocol model proxy.
_SUPPORTED_AGENTS = frozenset({"sdk.claude_sdk", "community.sade"})
_TUNNEL_READY_TIMEOUT_SEC = 30
# Sandbox lifetime beyond the agent budget, covering setup and teardown.
_LIFETIME_MARGIN_SEC = 1800
_UNBOUNDED_AGENT_LIFETIME_SEC = 6 * 3600
_SETUP_COMMAND_TIMEOUT_SEC = 900
_UPLOAD_TIMEOUT_SEC = 900


def _api_key(env_file: Path) -> str:
    """``E2B_API_KEY`` from the process env, else from the repo ``.env``."""
    value = os.environ.get(ENV_E2B_API_KEY, "").strip()
    if value:
        return value
    return (load_sandbox_env_values(env_file).get(ENV_E2B_API_KEY) or "").strip()


def _connection_opts(config: SandboxConfig) -> dict[str, str]:
    """E2B SDK connection parameters for *config*."""
    opts = {"api_key": _api_key(config.env_file)}
    if config.url:
        # Self-hosted services such as AgentENV serve the sandbox API at the
        # same URL; E2B cloud derives per-sandbox URLs from its domain instead.
        opts["api_url"] = config.url
        opts["sandbox_url"] = config.url
    return opts


_MEMORY_UNITS_MB = {
    "": 1 / (1024 * 1024),
    "k": 1 / 1024,
    "m": 1,
    "g": 1024,
    "t": 1024**2,
}


def memory_mb(value: str | None) -> int | None:
    """Convert a sandbox memory size such as ``4g`` or ``512m`` to MiB."""
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kmgt]?)(?:i?b)?\s*", value.lower())
    if not match:
        raise ValueError(f"Invalid sandbox memory size: {value!r}")
    return max(1, int(float(match.group(1)) * _MEMORY_UNITS_MB[match.group(2)]))


def e2b_template_name(
    template: str, image: str, *, cpus: int | None, memory: int | None
) -> str:
    """Template name for *image* at this sizing.

    The name changes with the pinned digest and the sizing, which E2B fixes
    when it builds a template, so a template built for another pin or size is
    never reused.
    """
    digest = image.rsplit("@sha256:", 1)[-1][:12]
    return f"nika-{template}-{digest}-c{cpus or 'auto'}-m{memory or 'auto'}"


def _require_supported(agent_type: str) -> None:
    if agent_type not in _SUPPORTED_AGENTS:
        raise RuntimeError(
            f"The e2b sandbox backend does not run {agent_type} yet "
            f"(supported: {', '.join(sorted(_SUPPORTED_AGENTS))})."
        )


def _normalized_machine(machine: str) -> str:
    machine = machine.strip().lower()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)


class _LineSplitter:
    """Turn stream chunks into complete lines."""

    def __init__(self, emit: Callable[[str], None]) -> None:
        self._emit = emit
        self._pending = ""

    def feed(self, chunk: str) -> None:
        self._pending += chunk
        *lines, self._pending = self._pending.split("\n")
        for line in lines:
            self._emit(line + "\n")

    def flush(self) -> None:
        if self._pending:
            self._emit(self._pending)
            self._pending = ""


class _CommandProcess:
    """:class:`StreamedProcess` over a background sandbox command."""

    def __init__(self, sandbox: Any, command: str) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._returncode: int | None = None
        self._handle = sandbox.commands.run(command, background=True, timeout=0)
        self._thread = threading.Thread(
            target=self._consume, name="nika-e2b-exec", daemon=True
        )
        self._thread.start()
        self.stdout: Iterator[str] = self._iter_lines()

    def _consume(self) -> None:
        from e2b.sandbox.commands.command_handle import CommandExitException

        out = _LineSplitter(self._lines.put)
        err = _LineSplitter(self._lines.put)
        try:
            result = self._handle.wait(on_stdout=out.feed, on_stderr=err.feed)
            code = result.exit_code
        except CommandExitException as exc:
            code = exc.exit_code
        except Exception as exc:  # noqa: BLE001 - surfaced as command output
            self._lines.put(f"Sandbox command stream failed: {exc}\n")
            code = 1
        out.flush()
        err.flush()
        self._returncode = code
        self._lines.put(None)

    def _iter_lines(self) -> Iterator[str]:
        while (line := self._lines.get()) is not None:
            yield line

    def wait(self) -> int:
        self._thread.join()
        return 1 if self._returncode is None else self._returncode

    def kill(self) -> None:
        try:
            self._handle.kill()
        except Exception as exc:  # noqa: BLE001 - already gone is fine
            logger.debug("Sandbox command kill failed: %s", exc)


class _CommandTunnel:
    """Run the tunnel relay in the sandbox and bridge it to host services."""

    def __init__(self, sandbox: Any, targets: Mapping[int, tuple[str, int]]) -> None:
        self._stopping = False
        ports = " ".join(str(port) for port in targets)
        self._handle = sandbox.commands.run(
            f"python3 -u {REMOTE_WORKSPACE}/{_RELAY_NAME} {ports}",
            background=True,
            stdin=True,
            timeout=0,
        )
        pid = self._handle.pid
        self.bridge = TunnelBridge(
            targets, send=lambda line: sandbox.commands.send_stdin(pid, line)
        )
        self._thread = threading.Thread(
            target=self._consume, name="nika-e2b-tunnel", daemon=True
        )
        self._thread.start()
        if not self.bridge.ready.wait(_TUNNEL_READY_TIMEOUT_SEC):
            self.stop()
            raise RuntimeError("The sandbox tunnel relay did not start")

    def _consume(self) -> None:
        try:
            self._handle.wait(
                on_stdout=self.bridge.feed,
                on_stderr=lambda data: logger.debug("tunnel relay: %s", data.rstrip()),
            )
        except Exception as exc:  # noqa: BLE001 - reported unless we stopped it
            if not self._stopping:
                logger.warning("The sandbox tunnel relay exited: %s", exc)

    def stop(self) -> None:
        self._stopping = True
        self.bridge.close()
        try:
            self._handle.kill()
        except Exception as exc:  # noqa: BLE001 - already gone is fine
            logger.debug("Tunnel relay kill failed: %s", exc)
        self._thread.join(timeout=5)


class E2bBackend(SandboxBackend):
    """Sandboxes created through the E2B SDK; see the module docstring."""

    name = "e2b"
    # The sandbox addresses its loopback (the tunnel relay); on the host only
    # the tunnel connects to the gateway.
    gateway_agent_host = "127.0.0.1"
    gateway_bind_host = "127.0.0.1"

    def __init__(self, config: SandboxConfig) -> None:
        super().__init__(config)
        self._host_stack = ExitStack()
        self._sandbox: Any = None
        self._tunnel: _CommandTunnel | None = None
        self._model_proxy_port: int | None = None
        self._template = ""

    def ensure_available(self) -> None:
        try:
            import e2b  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "The e2b sandbox backend needs the E2B SDK: uv sync --extra e2b"
            ) from exc
        if not _api_key(self.config.env_file):
            raise RuntimeError(
                f"Set {ENV_E2B_API_KEY} in {self.config.env_file} "
                "for the e2b sandbox backend."
            )

    def prepare_templates(self, agent_types: Sequence[str]) -> list[str]:
        # Templates are built by the service, which pulls the images itself.
        self.ensure_available()
        for agent_type in agent_types:
            _require_supported(agent_type)
        for template in dict.fromkeys(AGENT_TEMPLATES[a] for a in agent_types):
            self.ensure_template(template)
        return []

    def ensure_template(self, template: str) -> str:
        """Return the E2B template for *template*, built from its pinned image.

        Requires a service that implements the E2B template build API.
        """
        from e2b import Template

        image = TEMPLATE_IMAGES[template]
        cpus = self.config.cpus
        memory = memory_mb(self.config.memory)
        name = e2b_template_name(template, image, cpus=cpus, memory=memory)
        opts = _connection_opts(self.config)
        lock = Path.home() / ".local/state/nika/locks" / f"e2b-template-{name}.lock"
        with host_file_lock(lock):
            if Template.exists(name, **opts):
                return name
            started = time.perf_counter()
            log_event(
                "sandbox_template_build",
                f"Building e2b template {name} from {image}",
                template=name,
                image=image,
            )
            try:
                Template.build(
                    Template().from_image(image),
                    name,
                    cpu_count=cpus,
                    memory_mb=memory,
                    **opts,
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Could not build e2b template {name} from {image}. The e2b "
                    "backend needs a service that implements the E2B template "
                    f"build API: {exc}"
                ) from exc
            log_event(
                "sandbox_template_ready",
                f"Built e2b template {name}",
                template=name,
                image=image,
                duration_ms=elapsed_ms(started),
            )
        return name

    def prepare_host(
        self, *, agent_type: str, llm_provider: str | None
    ) -> dict[str, str]:
        from agent.cli.claude.vllm_shim import new_shim_token, vllm_messages_shim
        from agent.utils.provider_env import (
            CUSTOM_UNAUTHENTICATED_API_KEY,
            ENV_ANTHROPIC_API_KEY,
            ENV_ANTHROPIC_AUTH_TOKEN,
            ENV_ANTHROPIC_BASE_URL,
        )

        _require_supported(agent_type)
        if not llm_provider:
            raise RuntimeError("Set agent.provider for sandboxed agents.")
        self._template = self.ensure_template(AGENT_TEMPLATES[agent_type])

        sources = load_credential_sources(
            self.config.env_file, provider=llm_provider, agent_type=agent_type
        )
        upstream = sources.get(ENV_ANTHROPIC_BASE_URL) or _ANTHROPIC_API_URL
        api_key = sources.get(ENV_ANTHROPIC_API_KEY) or sources.get(
            ENV_ANTHROPIC_AUTH_TOKEN, ""
        )
        if api_key == CUSTOM_UNAUTHENTICATED_API_KEY:
            api_key = ""
        if not api_key and llm_provider != "custom":
            raise RuntimeError(
                f"No API key found for provider {llm_provider!r} in "
                f"{self.config.env_file}."
            )
        # Model proxy on host loopback: the tunnel is its only client. It
        # checks the per-session token and sends the real key upstream.
        token = new_shim_token()
        self._model_proxy_port = self._host_stack.enter_context(
            vllm_messages_shim(
                upstream, bind_host="127.0.0.1", token=token, api_key=api_key
            )
        )
        proxy_url = f"http://127.0.0.1:{self._model_proxy_port}"
        return {
            ENV_ANTHROPIC_BASE_URL: proxy_url,
            ENV_ANTHROPIC_API_KEY: token,
            ENV_ANTHROPIC_AUTH_TOKEN: token,
        }

    def workspace_path(self, workspace_dir: Path) -> str:
        return REMOTE_WORKSPACE

    def stage_workspace(self, workspace_dir: Path, *, agent_type: str) -> None:
        shutil.copyfile(RELAY_SCRIPT, workspace_dir / _RELAY_NAME)
        if agent_type in SDK_AGENT_TYPES:
            # Always offline: the sandbox has no internet access.
            stage_sdk_wheels(workspace_dir)
            stage_sdk_requirements(workspace_dir)

    def _lifetime_sec(self) -> int:
        from nika.utils.agent_config import resolve_agent_timeout

        budget = resolve_agent_timeout()
        if budget <= 0:
            budget = _UNBOUNDED_AGENT_LIFETIME_SEC
        return budget + _LIFETIME_MARGIN_SEC

    def _run_checked(self, command: str, *, timeout: int) -> str:
        from e2b.sandbox.commands.command_handle import CommandExitException

        try:
            result = self._sandbox.commands.run(command, timeout=timeout)
        except CommandExitException as exc:
            detail = (getattr(exc, "stdout", "") or "") + (exc.stderr or "")
            raise RuntimeError(
                f"Sandbox command failed with exit code {exc.exit_code}: "
                f"{command}\n{detail.strip()}"
            ) from exc
        return result.stdout

    def _upload_workspace(self, workspace_dir: Path) -> None:
        archive_path = f"{REMOTE_WORKSPACE}.tar"
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                tar.add(workspace_dir, arcname=".")
            archive.seek(0)
            self._sandbox.files.write(
                archive_path, archive, request_timeout=_UPLOAD_TIMEOUT_SEC
            )
        self._run_checked(
            f"mkdir -p {REMOTE_WORKSPACE} && "
            f"tar -xf {archive_path} -C {REMOTE_WORKSPACE} && rm -f {archive_path}",
            timeout=_SETUP_COMMAND_TIMEOUT_SEC,
        )

    def _check_architecture(self) -> None:
        """Offline wheels are built for the host; the sandbox must match."""
        remote = _normalized_machine(self._run_checked("uname -m", timeout=60))
        host = _normalized_machine(platform.machine())
        if remote != host:
            raise RuntimeError(
                f"Template {self._template!r} runs on {remote}, but the SDK wheels "
                f"are staged for this {host} host."
            )

    def create(
        self, *, agent_session_id: str, agent_type: str, workspace_dir: Path
    ) -> str:
        from e2b import Sandbox

        self._sandbox = Sandbox.create(
            template=self._template,
            timeout=self._lifetime_sec(),
            metadata={_METADATA_SESSION_KEY: agent_session_id},
            allow_internet_access=False,
            **_connection_opts(self.config),
        )
        try:
            if agent_type in SDK_AGENT_TYPES:
                self._check_architecture()
            self._upload_workspace(workspace_dir)
            if agent_type in SDK_AGENT_TYPES:
                self._run_checked(
                    sdk_pip_install_command(REMOTE_WORKSPACE, offline=True),
                    timeout=_SETUP_COMMAND_TIMEOUT_SEC,
                )
        except BaseException:
            if not self.config.keep_container:
                self._kill_sandbox()
            raise
        return self._sandbox.sandbox_id

    def allow_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        targets = {port: ("127.0.0.1", port)}
        if self._model_proxy_port is not None:
            targets[self._model_proxy_port] = ("127.0.0.1", self._model_proxy_port)
        self._tunnel = _CommandTunnel(self._sandbox, targets)

    def revoke_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        self._stop_tunnel()

    def exec_stream(self, sandbox_id: str, command: str) -> StreamedProcess:
        return _CommandProcess(self._sandbox, command)

    def push_files(
        self, sandbox_id: str, workspace_dir: Path, names: Sequence[str]
    ) -> None:
        for name in names:
            self._sandbox.files.write(
                f"{REMOTE_WORKSPACE}/{name}", (workspace_dir / name).read_bytes()
            )

    def pull_files(
        self, sandbox_id: str, workspace_dir: Path, names: Sequence[str]
    ) -> None:
        from e2b.exceptions import NotFoundException

        for name in names:
            try:
                data = self._sandbox.files.read(
                    f"{REMOTE_WORKSPACE}/{name}", format="bytes"
                )
            except NotFoundException:
                continue
            write_workspace_bytes(workspace_dir / name, bytes(data))

    def remove(self, sandbox_id: str) -> None:
        self._stop_tunnel()
        self._kill_sandbox()

    def release_host(self) -> None:
        self._stop_tunnel()
        self._host_stack.close()
        self._model_proxy_port = None

    def start_log_fields(self) -> dict[str, Any]:
        return {"e2b_template": self._template, "e2b_url": self.config.url}

    def _stop_tunnel(self) -> None:
        if self._tunnel is not None:
            self._tunnel.stop()
            self._tunnel = None

    def _kill_sandbox(self) -> None:
        if self._sandbox is None:
            return
        try:
            self._sandbox.kill()
        except Exception as exc:  # noqa: BLE001 - teardown continues
            logger.warning("Could not kill e2b sandbox: %s", exc)

    @classmethod
    def cleanup_session(
        cls, session_meta: Mapping[str, Any]
    ) -> SandboxCleanupResult | None:
        from agent.sandbox.config import resolve_sandbox_config
        from agent.sandbox.workspace import opaque_agent_workspace_dir
        from nika.utils.agent_session_id import resolve_agent_session_id

        agent_sid = resolve_agent_session_id(session_meta)
        if not agent_sid:
            return None
        removed_sandbox = False
        config = replace(resolve_sandbox_config(), url=session_meta.get("sandbox_url"))
        opts = _connection_opts(config)
        if opts["api_key"]:
            try:
                from e2b import Sandbox, SandboxQuery
            except ImportError:
                Sandbox = None  # noqa: N806 - SDK missing: nothing to reach
            if Sandbox is not None:
                query = SandboxQuery(metadata={_METADATA_SESSION_KEY: agent_sid})
                for info in Sandbox.list(query=query, **opts).next_items():
                    removed_sandbox |= bool(Sandbox.kill(info.sandbox_id, **opts))
        workspace = opaque_agent_workspace_dir(agent_sid)
        removed_workspace = False
        if workspace.is_dir():
            shutil.rmtree(workspace, ignore_errors=True)
            removed_workspace = not workspace.exists()
        return SandboxCleanupResult(
            sandbox_name=agent_sid,
            removed_sandbox=removed_sandbox,
            removed_workspace=removed_workspace,
        )
