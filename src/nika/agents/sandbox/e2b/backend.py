"""E2B-compatible backend: sandboxes on E2B or a self-hosted service (AgentENV).

These sandboxes have no route to the NIKA host (AgentENV denies private
ranges at node level) and no internet access (created with
``allow_internet_access=False``, checked after create). Agent packages are
installed when the service builds the template, so the sandbox itself never
needs egress, and an added agent's declared ``sandbox.egress`` is not used.
The agent reaches its MCP gateway and the host model proxy through the
tunnel in :mod:`nika.agents.sandbox.tunnel`, which runs over the same command
channel NIKA uses to drive the sandbox. With a remote
lab, the host end of the tunnel connects to the lab host's gateway. The model
proxy adds the real provider key on the host, so the sandbox only holds a
per-session token.

The workspace is uploaded to the same absolute path it has on the host, as
Docker Sandboxes mounts it, so commands and generated agent configs need no
path translation. Sandboxes run as the template image's user, in its working
directory and with its environment (see :mod:`nika.agents.sandbox.e2b.image_config`).
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import shlex
import shutil
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nika.agents.sandbox.backend import (
    SandboxBackend,
    SandboxCleanupResult,
    StreamedProcess,
)
from nika.agents.sandbox.model_calls import ModelCallLog
from nika.agents.sandbox.config import (
    SandboxConfig,
    load_sandbox_env_values,
)
from nika.agents.sandbox.e2b.image_config import image_config
from nika.agents.sandbox.locks import host_file_lock
from nika.agents.sandbox.templates import TEMPLATE_IMAGES, agent_template
from nika.agents.sandbox.tunnel import RELAY_SCRIPT, TunnelBridge
from nika.agents.sandbox.npm import stage_npm
from nika.agents.sandbox.wheels import (
    load_requirements,
    needs_agent_venv,
    venv_install_commands,
)
from nika.agents.sandbox.workspace import write_workspace_bytes
from nika.utils.logger import elapsed_ms, log_event

logger = logging.getLogger(__name__)

ENV_E2B_API_KEY = "E2B_API_KEY"
ENV_E2B_DOMAIN = "E2B_DOMAIN"
_RELAY_NAME = "tunnel_relay.py"
_METADATA_SESSION_KEY = "nika_agent_session_id"
_TUNNEL_READY_TIMEOUT_SEC = 30
# Waits before each relay restart after the E2B channel drops; then give up.
_TUNNEL_RESTART_DELAYS_SEC = (1, 2, 5, 10, 20, 30)
# Sandbox lifetime beyond the agent budget, covering setup and teardown.
_LIFETIME_MARGIN_SEC = 1800
_UNBOUNDED_AGENT_LIFETIME_SEC = 6 * 3600
_SETUP_COMMAND_TIMEOUT_SEC = 900
_UPLOAD_TIMEOUT_SEC = 900
# Fails when the sandbox opens a connection to the internet (by name or by
# address): the service must enforce ``allow_internet_access=False``.
_EGRESS_PROBE = (
    "python3 -c 'import socket,sys\n"
    'for host in ("example.com", "1.1.1.1"):\n'
    "    try:\n"
    "        socket.create_connection((host, 443), timeout=3).close()\n"
    "    except OSError:\n"
    "        continue\n"
    '    sys.exit("reached " + host)\n\''
)
# Disable page reclaim in microVMs that have it enabled
_DISABLE_PAGE_RECLAIM = (
    "f=/sys/module/damon_reclaim/parameters/enabled; "
    'if [ -w "$f" ]; then echo N > "$f"; fi'
)


def _env_setting(name: str, env_file: Path) -> str:
    """*name* from the process env, else from the repo ``.env``."""
    value = os.environ.get(name, "").strip()
    if value:
        return value
    return (load_sandbox_env_values(env_file).get(name) or "").strip()


def _api_key(env_file: Path) -> str:
    return _env_setting(ENV_E2B_API_KEY, env_file)


def _connection_opts(
    config: SandboxConfig, *, domain: str | None = None
) -> dict[str, str]:
    """E2B SDK connection parameters for *config*.

    A service URL takes every request: serving API and all sandboxes at
    one address and route by request headers.
    Otherwise ``E2B_DOMAIN`` selects a self-hosted E2B, whose API and
    sandbox host names the SDK derives from it; without either, E2B cloud.
    *domain* replaces ``E2B_DOMAIN`` ("" for E2B cloud).
    """
    opts = {"api_key": _api_key(config.env_file)}
    if domain is None:
        domain = _env_setting(ENV_E2B_DOMAIN, config.env_file)
    if config.url:
        opts["api_url"] = config.url
        opts["sandbox_url"] = config.url
    elif domain:
        opts["domain"] = domain
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
    template: str,
    image: str,
    *,
    cpus: int | None,
    memory: int | None,
    packages: Sequence[str] = (),
    requires_python: str | None = None,
) -> str:
    """Template name for *image* at this sizing, with these Python *packages*.

    The name changes with the pinned digest, the sizing, the package pins and
    the Python they need, which E2B fixes when it builds a template, so a
    template built for other pins or another size is never reused.
    """
    digest = image.rsplit("@sha256:", 1)[-1][:12]
    name = f"nika-{template}-{digest}"
    if packages:
        lines = list(packages)
        if needs_agent_venv(requires_python):
            lines.append(f"python{requires_python}")
        pins = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:8]
        name = f"{name}-py{pins}"
    return f"{name}-c{cpus or 'auto'}-m{memory or 'auto'}"


def _template_entry(api: Any, name: str) -> dict[str, Any] | None:
    """The service's entry for the template named *name*, if any.

    Read as plain JSON: the SDK's typed model rejects entries that
    self-hosted services shorten.
    """
    response = api.get("/templates")
    response.raise_for_status()
    for entry in response.json():
        if name in [*(entry.get("names") or []), *(entry.get("aliases") or [])]:
            return entry
    return None


def _wait_streaming(
    handle: Any,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
) -> Any:
    """``handle.wait()`` that passes output on without keeping it.

    The SDK also collects all output for the command result. The agent and
    tunnel relay commands run for a whole session, so that copy would only
    grow; NIKA never reads it.
    """

    def forward(callback: Callable[[str], None], kept: str) -> Callable[[str], None]:
        def feed(data: str) -> None:
            callback(data)
            chunks = getattr(handle, kept, None)
            if isinstance(chunks, list):
                chunks.clear()

        return feed

    return handle.wait(
        on_stdout=forward(on_stdout, "_stdout_chunks"),
        on_stderr=forward(on_stderr, "_stderr_chunks"),
    )


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
            result = _wait_streaming(self._handle, out.feed, err.feed)
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
    """Run the tunnel relay in the sandbox and bridge it to host services.

    The relay reaches the host over E2B API connections, which can drop. A
    failed send or a lost output stream restarts the relay on the same ports:
    open connections close, and clients reconnect. Resending lost frames
    instead could deliver some twice.
    """

    def __init__(
        self, sandbox: Any, relay: str, targets: Mapping[int, tuple[str, int]]
    ) -> None:
        self._sandbox = sandbox
        self._targets = dict(targets)
        ports = " ".join(str(port) for port in targets)
        self._command = f"python3 -u {shlex.quote(relay)} {ports}"
        self._stopping = threading.Event()
        self._lost = threading.Event()
        # Serializes starting and discarding relays (keeper versus stop()).
        self._lock = threading.Lock()
        self._handle: Any = None
        # A relay to kill before the next start (a failed kill is retried).
        self._stale: Any = None
        self._reader: threading.Thread | None = None
        self._keeper: threading.Thread | None = None
        self.bridge: TunnelBridge | None = None
        try:
            self._start()
        except Exception:
            self.stop()
            raise
        self._keeper = threading.Thread(
            target=self._keep_alive, name="nika-e2b-tunnel", daemon=True
        )
        self._keeper.start()

    def _start(self) -> None:
        """Start a relay and wait until it listens on its ports."""
        with self._lock:
            if self._stopping.is_set():
                raise RuntimeError("The sandbox tunnel is stopping")
            handle = self._sandbox.commands.run(
                self._command, background=True, stdin=True, timeout=0
            )
            bridge = TunnelBridge(
                self._targets, send=lambda line: self._send(handle, line)
            )
            self._handle, self.bridge = handle, bridge
            self._reader = threading.Thread(
                target=self._consume,
                args=(handle, bridge),
                name="nika-e2b-relay",
                daemon=True,
            )
            self._reader.start()
        deadline = time.monotonic() + _TUNNEL_READY_TIMEOUT_SEC
        while not bridge.ready.wait(0.2):
            if self._lost.is_set() or time.monotonic() > deadline:
                raise RuntimeError("The sandbox tunnel relay did not start")

    def _send(self, handle: Any, line: str) -> None:
        try:
            self._sandbox.commands.send_stdin(handle.pid, line)
        except Exception:
            self._channel_lost(handle)
            raise

    def _consume(self, handle: Any, bridge: TunnelBridge) -> None:
        try:
            _wait_streaming(
                handle,
                bridge.feed,
                lambda data: logger.debug("tunnel relay: %s", data.rstrip()),
            )
            reason = "the relay exited"
        except Exception as exc:  # noqa: BLE001 - the keeper restarts the relay
            reason = str(exc)
        if handle is self._handle and not self._stopping.is_set():
            logger.warning("Lost the sandbox tunnel relay: %s", reason)
        self._channel_lost(handle)

    def _channel_lost(self, handle: Any) -> None:
        # Ignore relays already replaced or stopped on purpose.
        if handle is self._handle and not self._stopping.is_set():
            self._lost.set()

    def _keep_alive(self) -> None:
        while True:
            self._lost.wait()
            if self._stopping.is_set():
                return
            if not self._restart():
                if not self._stopping.is_set():
                    logger.error(
                        "The sandbox tunnel stays down: its relay won't restart"
                    )
                return

    def _restart(self) -> bool:
        for delay in _TUNNEL_RESTART_DELAYS_SEC:
            if self._stopping.wait(delay):
                return False
            if not self._discard():
                continue
            self._lost.clear()
            try:
                self._start()
            except Exception as exc:  # noqa: BLE001 - retried after the next delay
                logger.warning("Could not restart the sandbox tunnel relay: %s", exc)
                continue
            logger.warning(
                "Restarted the sandbox tunnel relay; its open connections were closed"
            )
            return True
        return False

    def _discard(self) -> bool:
        """Close the bridge and kill the relay; False when the kill failed."""
        with self._lock:
            if self.bridge is not None:
                self.bridge.close()
            handle, self._handle = self._handle, None
            if handle is not None:
                self._stale = handle
            if self._stale is None:
                return True
            try:
                self._stale.kill()
            except Exception as exc:  # noqa: BLE001 - retried before the next start
                logger.debug("Tunnel relay kill failed: %s", exc)
                return False
            self._stale = None
            return True

    def stop(self) -> None:
        self._stopping.set()
        self._lost.set()
        self._discard()
        for thread in (self._keeper, self._reader):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=5)


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
        self._workspace = ""

    def ensure_available(self) -> None:
        try:
            import e2b  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "The e2b sandbox backend needs the E2B SDK: run uv sync"
            ) from exc
        if not _api_key(self.config.env_file):
            raise RuntimeError(
                f"Set {ENV_E2B_API_KEY} in {self.config.env_file} "
                "for the e2b sandbox backend."
            )

    def prepare_templates(self, agent_types: Sequence[str]) -> list[str]:
        # Templates are built by the service, which pulls the images itself.
        self.ensure_available()
        for agent_type in dict.fromkeys(agent_types):
            self.ensure_agent_template(agent_type)
        return []

    def ensure_agent_template(self, agent_type: str) -> str:
        """The E2B template of *agent_type*, with its Python packages."""
        from nika.agents.registry import get_agent_spec

        spec = get_agent_spec(agent_type)
        if spec.template is None:
            raise ValueError(f"Agent {agent_type!r} does not run in a sandbox")
        return self.ensure_template(
            spec.template,
            packages=load_requirements(spec.requirements) + spec.packages,
            requires_python=spec.requires_python,
        )

    def ensure_template(
        self,
        template: str,
        *,
        packages: Sequence[str] = (),
        requires_python: str | None = None,
    ) -> str:
        """Return the E2B template for *template*, built from its pinned image.

        Requires a service that implements the E2B template build API.
        """
        from e2b import Template
        from e2b.api.client_sync import get_api_client
        from e2b.connection_config import ConnectionConfig

        image = TEMPLATE_IMAGES[template]
        cpus = self.config.cpus
        memory = memory_mb(self.config.memory)
        name = e2b_template_name(
            template,
            image,
            cpus=cpus,
            memory=memory,
            packages=packages,
            requires_python=requires_python,
        )
        opts = _connection_opts(self.config)
        api = get_api_client(ConnectionConfig(**opts)).get_httpx_client()
        lock = Path.home() / ".local/state/nika/locks" / f"e2b-template-{name}.lock"
        with host_file_lock(lock):
            entry = _template_entry(api, name)
            if entry is not None and entry.get("buildStatus") == "ready":
                return name
            if entry is not None and entry.get("buildStatus") == "error":
                # A failed build keeps the name, which a new build cannot take.
                api.delete(f"/templates/{entry['templateID']}").raise_for_status()
            config = image_config(image)
            builder = Template().from_image(image)
            if packages and needs_agent_venv(requires_python):
                assert requires_python is not None
                # The template's Python does not fit: a uv venv with one that does.
                for command in venv_install_commands(
                    requires_python, packages=packages, no_cache=True
                ):
                    builder = builder.run_cmd(command, user=config.user)
            elif packages:
                # The service downloads the packages while it builds, so
                # sandboxes start ready and this host uploads nothing large.
                # As the image user, as on sbx: root cannot replace packages
                # the distribution installed.
                builder = builder.run_cmd(
                    "pip3 install --break-system-packages --no-cache-dir "
                    "--retries 10 --timeout 120 "
                    + " ".join(shlex.quote(pin) for pin in packages),
                    user=config.user,
                )
            started = time.perf_counter()
            log_event(
                "sandbox_template_build",
                f"Building e2b template {name} from {image}",
                template=name,
                image=image,
            )
            try:
                Template.build(
                    builder.set_user(config.user).set_workdir(config.workdir),
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
        self,
        *,
        agent_type: str,
        llm_provider: str | None,
        call_log: ModelCallLog | None = None,
    ) -> dict[str, str]:
        from nika.agents.sandbox.model_proxy import start_model_proxy

        if not llm_provider:
            raise RuntimeError("Set agent.provider for sandboxed agents.")
        self._template = self.ensure_agent_template(agent_type)
        # Model proxy on host loopback: the tunnel is its only client. It
        # checks the per-session token and sends the real key upstream.
        self._model_proxy_port, env = start_model_proxy(
            self._host_stack,
            env_file=self.config.env_file,
            agent_type=agent_type,
            provider=llm_provider,
            bind_host="127.0.0.1",
            agent_host="127.0.0.1",
            call_log=call_log,
        )
        return env

    def stage_workspace(self, workspace_dir: Path, *, agent_type: str) -> None:
        # Agent packages come with the template (see ensure_agent_template).
        shutil.copyfile(RELAY_SCRIPT, workspace_dir / _RELAY_NAME)
        from nika.agents.registry import get_agent_spec

        spec = get_agent_spec(agent_type)
        stage_npm(workspace_dir, spec.npm_packages)

    def _lifetime_sec(self) -> int:
        from nika.utils.agent_config import resolve_agent_timeout

        budget = resolve_agent_timeout()
        if budget <= 0:
            budget = _UNBOUNDED_AGENT_LIFETIME_SEC
        return budget + _LIFETIME_MARGIN_SEC

    def _run_checked(
        self, command: str, *, timeout: int, user: str | None = None
    ) -> str:
        from e2b.sandbox.commands.command_handle import CommandExitException

        try:
            result = self._sandbox.commands.run(command, timeout=timeout, user=user)
        except CommandExitException as exc:
            detail = (getattr(exc, "stdout", "") or "") + (exc.stderr or "")
            raise RuntimeError(
                f"Sandbox command failed with exit code {exc.exit_code}: "
                f"{command}\n{detail.strip()}"
            ) from exc
        return result.stdout

    def _create_workspace_dir(self, workspace: str) -> None:
        """Create the host workspace path in the sandbox, owned by its user."""
        owner = self._run_checked("id -un", timeout=60).strip()
        self._run_checked(
            f"mkdir -p {shlex.quote(workspace)} && "
            f"chown {shlex.quote(owner)}: {shlex.quote(workspace)}",
            timeout=60,
            user="root",
        )

    def _upload(self, workspace_dir: Path, names: Sequence[str]) -> None:
        """Copy files or directories under *workspace_dir* to the same path."""
        archive_path = f"/tmp/upload-{uuid.uuid4().hex}.tar"
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                for name in names:
                    tar.add(workspace_dir / name, arcname=name)
            archive.seek(0)
            self._sandbox.files.write(
                archive_path, archive, request_timeout=_UPLOAD_TIMEOUT_SEC
            )
        self._run_checked(
            f"tar -xf {archive_path} -C {shlex.quote(str(workspace_dir))} "
            f"&& rm -f {archive_path}",
            timeout=_SETUP_COMMAND_TIMEOUT_SEC,
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
            envs=image_config(TEMPLATE_IMAGES[agent_template(agent_type)]).env,
            **_connection_opts(self.config),
        )
        self._workspace = str(workspace_dir)
        try:
            self._run_checked(_DISABLE_PAGE_RECLAIM, timeout=60, user="root")
            self._create_workspace_dir(self._workspace)
            self._upload(workspace_dir, ["."])
            self._check_no_egress()
        except BaseException:
            if not self.config.keep_container:
                self._kill_sandbox()
            raise
        return self._sandbox.sandbox_id

    def _check_no_egress(self) -> None:
        """Refuse a sandbox that can reach the internet (fail closed)."""
        try:
            self._run_checked(_EGRESS_PROBE, timeout=60)
        except RuntimeError as exc:
            raise RuntimeError(
                "The e2b sandbox has internet access although it was created "
                "with allow_internet_access=False; the sandbox service does "
                f"not enforce egress isolation: {exc}"
            ) from exc

    def sandbox_gateway_url(self, *, port: int, gateway_url: str) -> str:
        # The tunnel relay on the sandbox's loopback forwards to the gateway.
        return f"http://127.0.0.1:{self._relay_gateway_port(port)}"

    def allow_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        # The tunnel connects from this host, to a local gateway on loopback or
        # to a remote lab host's gateway, so the sandbox still has no network.
        host = urlparse(gateway_url).hostname or "127.0.0.1"
        targets = {self._relay_gateway_port(port): (host, port)}
        if self._model_proxy_port is not None:
            targets[self._model_proxy_port] = ("127.0.0.1", self._model_proxy_port)
        relay = f"{self._workspace}/{_RELAY_NAME}"
        self._tunnel = _CommandTunnel(self._sandbox, relay, targets)

    def _relay_gateway_port(self, port: int) -> int:
        """Sandbox port that the relay forwards to a gateway on *port*.

        The relay also listens on the model proxy's port, which a remote lab
        host's gateway can share. Both are ephemeral ports, so the port below
        is unprivileged and free.
        """
        return port - 1 if port == self._model_proxy_port else port

    def revoke_gateway(self, sandbox_id: str, *, port: int, gateway_url: str) -> None:
        self._stop_tunnel()

    def exec_stream(self, sandbox_id: str, command: str) -> StreamedProcess:
        return _CommandProcess(self._sandbox, command)

    def push_files(
        self, sandbox_id: str, workspace_dir: Path, names: Sequence[str]
    ) -> None:
        self._upload(workspace_dir, names)

    def pull_files(
        self, sandbox_id: str, workspace_dir: Path, names: Sequence[str]
    ) -> None:
        from e2b.exceptions import NotFoundException

        for name in names:
            try:
                data = self._sandbox.files.read(
                    f"{workspace_dir}/{name}", format="bytes"
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
    def session_fields(cls, config: SandboxConfig) -> dict[str, str]:
        if config.url:
            return {"sandbox_url": config.url}
        domain = _env_setting(ENV_E2B_DOMAIN, config.env_file)
        return {"sandbox_domain": domain} if domain else {}

    @classmethod
    def cleanup_session(
        cls, session_meta: Mapping[str, Any]
    ) -> SandboxCleanupResult | None:
        from nika.agents.sandbox.config import resolve_sandbox_config
        from nika.agents.sandbox.workspace import opaque_agent_workspace_dir
        from nika.utils.agent_session_id import resolve_agent_session_id

        agent_sid = resolve_agent_session_id(session_meta)
        if not agent_sid:
            return None
        removed_sandbox = False
        # Reach the service the session ran on (see session_fields).
        config = replace(resolve_sandbox_config(), url=session_meta.get("sandbox_url"))
        opts = _connection_opts(config, domain=session_meta.get("sandbox_domain") or "")
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
