"""Codex CLI subprocess adapter for diagnosis/submission phases.

Each ``CodexWorker`` instance drives one ``codex exec`` invocation inside an
isolated, per-session workspace.  It handles:

* **Workspace creation** – ``{session_dir}/codex_workspace/`` (git-initialised
  so Codex is happy; safe to call multiple times).
* **CODEX_HOME isolation** – a private ``.codex_home/`` inside the workspace is
  used as ``CODEX_HOME``, so no files are written to ``~/.codex/``.
  ``auth.json`` is sym-linked from the user's real ``~/.codex/auth.json`` so
  that authentication still works.
* **MCP server config** – ``config.toml`` in the isolated home contains only
  the servers relevant to the current phase and scenario (selected by
  :func:`~agent.utils.mcp_servers.select_diagnosis_servers`).
* **Session ID propagation** – ``NIKA_SESSION_ID`` is injected into every MCP
  server's ``env`` block, exactly as :class:`~agent.utils.mcp_servers.MCPServerConfig`
  does for the LangChain path.
* **Output capture** – the final assistant message is written by
  ``--output-last-message``; JSONL events emitted via ``--json`` are streamed
  line-by-line, logged to ``messages.jsonl`` in real time, and pretty-printed to
  the terminal via :func:`~agent.cli.codex.codex_display.format_codex_event`.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

from agent.cli.codex.codex_display import format_codex_event
from agent.utils.loggers import MessageLogger
from agent.sandbox.sbx.auth import apply_codex_auth
from agent.sandbox.sbx.exec import exec_in_sandbox, sandbox_name_from_env
from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config
from agent.protocols import PHASES, SUBMISSION
from agent.utils.skills import prepare_codex_workspace
from agent.utils.provider_env import build_agent_subprocess_env

REASONING_EFFORT_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh")
DEFAULT_STALL_TIMEOUT_S = 300
RECONNECT_STALL_TIMEOUT_S = 120


def prepare_codex_subprocess_env(
    *,
    codex_home: str | Path,
    provider: str,
    agent_type: str = "cli.codex",
    base: dict[str, str] | None = None,
) -> dict[str, str]:
    """Minimal env for ``codex exec`` with provider-mapped credentials only."""
    if not provider or not str(provider).strip():
        raise ValueError(
            "Missing LLM provider: set agent.provider in config/nika.yaml "
            "or pass -p/--provider."
        )
    prov = str(provider).strip().lower()
    env = build_agent_subprocess_env(agent_type=agent_type, provider=prov, base=base)
    env["CODEX_HOME"] = str(codex_home)
    return env


class CodexSubprocessStallError(Exception):
    """Raised when Codex stops making progress (e.g. reconnect loops)."""

    def __init__(self, *, stall_s: int, reconnect_failure: bool) -> None:
        self.stall_s = stall_s
        self.reconnect_failure = reconnect_failure
        reason = (
            "Codex reconnect attempts failed"
            if reconnect_failure
            else "no Codex progress"
        )
        super().__init__(f"stalled after {stall_s}s without {reason}")


def _is_productive_codex_event(event: dict) -> bool:
    """Return True when a JSONL event indicates real agent work, not a reconnect."""
    event_type = event.get("type", "")
    if event_type in {"thread.started", "turn.completed"}:
        return True
    if event_type == "item.completed":
        return (event.get("item") or {}).get("type") not in {"error"}
    if event_type == "item.started":
        return (event.get("item") or {}).get("type") in {
            "mcp_tool_call",
            "command_execution",
            "agent_message",
        }
    return False


def _reconnect_transport_failed(event: dict) -> bool:
    """Return True when Codex exhausted reconnect attempts or fell back transport."""
    if event.get("type") == "error":
        return "Reconnecting... 5/5" in event.get("message", "")
    if event.get("type") == "item.completed":
        item = event.get("item") or {}
        if item.get("type") == "error":
            message = item.get("message", "")
            return "Falling back" in message or "timed out" in message.lower()
    return False


# ---------------------------------------------------------------------------
# TOML helper
# ---------------------------------------------------------------------------


def _build_mcp_toml(servers: dict) -> str:
    """Serialise an MCP server dict (from MCPServerConfig) as TOML."""
    lines: list[str] = [
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
        "",
        "[sandbox_workspace_write]",
        "network_access = true",
        "",
    ]
    for name, srv in servers.items():
        lines.append(f"[mcp_servers.{name}]")
        if srv.get("transport") == "http":
            lines.append(f'url = "{srv["url"]}"')
            # A troubleshooting run without its MCP tools can appear to finish
            # normally while producing no submission.  Make that startup
            # failure explicit instead of letting Codex continue tool-less.
            lines.append("required = true")
            lines.append('default_tools_approval_mode = "approve"')
            headers: dict = srv.get("headers") or {}
            if headers:
                lines.append(f"\n[mcp_servers.{name}.http_headers]")
                for k, v in headers.items():
                    lines.append(f'{k} = "{v}"')
        else:
            lines.append(f'command = "{srv["command"]}"')
            args_toml = "[" + ", ".join(f'"{a}"' for a in srv["args"]) + "]"
            lines.append(f"args = {args_toml}")
            lines.append('default_tools_approval_mode = "approve"')
            env: dict = srv.get("env", {})
            if env:
                lines.append(f"\n[mcp_servers.{name}.env]")
                for k, v in env.items():
                    lines.append(f'{k} = "{v}"')
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CodexWorker
# ---------------------------------------------------------------------------


class CodexWorker:
    """Run one non-interactive ``codex exec`` invocation for a pipeline phase.

    Parameters
    ----------
    session_id:
        NIKA session identifier — resolves the session directory and is
        propagated to MCP servers via ``NIKA_SESSION_ID``.
    session_dir:
        Absolute path to the session results directory.
    phase:
        One of :data:`~agent.protocols.PHASES` (``diagnosis`` or ``submission``).
    model:
        Codex model name forwarded to ``codex exec -m``.
    reasoning_effort:
        Optional Codex ``model_reasoning_effort`` override forwarded via
        ``codex exec -c model_reasoning_effort=...``.
    timeout:
        Hard timeout in seconds for the subprocess (default 600 s).
    stall_timeout:
        Kill the subprocess when no productive Codex events arrive for this
        many seconds (default 300 s).  After reconnect exhaustion the limit
        drops to :data:`RECONNECT_STALL_TIMEOUT_S`.
    llm_provider:
        Active LLM provider for credential mapping.
    scenario_name:
        Used by :func:`~agent.utils.mcp_servers.select_diagnosis_servers` to pick relevant servers.
        Ignored for the submission phase (which always uses the task server).
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        phase: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        timeout: int = 600,
        stall_timeout: int = DEFAULT_STALL_TIMEOUT_S,
        scenario_name: str = "",
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
        if (
            reasoning_effort is not None
            and reasoning_effort not in REASONING_EFFORT_LEVELS
        ):
            raise ValueError(
                f"reasoning_effort must be one of {REASONING_EFFORT_LEVELS}, got {reasoning_effort!r}"
            )

        self.session_id = session_id
        self.phase = phase
        self.model = model
        self.llm_provider = llm_provider
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.stall_timeout = stall_timeout
        self.scenario_name = scenario_name
        self._reconnect_failure_at: float | None = None
        self._last_progress_at: float | None = None

        self.session_dir = Path(session_dir)
        self.workspace = self.session_dir / "codex_workspace"
        self._codex_home = self.workspace / ".codex_home"
        self._logger = MessageLogger(agent=phase, session_dir=session_dir)
        self._stream_output = stream_output

    # ------------------------------------------------------------------
    # Workspace + isolated CODEX_HOME setup
    # ------------------------------------------------------------------

    def _setup_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._codex_home.mkdir(parents=True, exist_ok=True)

        # Initialise a git repo so Codex doesn't complain.
        if not (self.workspace / ".git").exists():
            subprocess.run(
                ["git", "init", "-q"],
                cwd=self.workspace,
                check=True,
                capture_output=True,
            )

        # Populate auth.json from staged host auth, host symlink, or env API key.
        apply_codex_auth(self._codex_home)

        prepare_codex_workspace(self.workspace)
        self._write_mcp_config()

    def _write_mcp_config(self) -> None:
        if self.phase == SUBMISSION:
            begin_submission_mcp_phase(self.session_id)
        servers = load_session_mcp_config(
            self.session_id,
            self.scenario_name,
            session_dir=self.session_dir,
        )
        # Give each Codex subprocess only the servers it can use in its
        # current phase.  The gateway enforces this too, but excluding the
        # task server here keeps the diagnosis prompt and tool inventory free
        # of submission-only fault catalog metadata.
        from nika.service.mcp_server.registry import SUBMISSION_SERVER

        if self.phase == SUBMISSION:
            servers = {
                name: config
                for name, config in servers.items()
                if name == SUBMISSION_SERVER
            }
        else:
            servers = {
                name: config
                for name, config in servers.items()
                if name != SUBMISSION_SERVER
            }

        self._logger.log(
            "mcp_config",
            {"phase": self.phase, "servers": list(servers.keys())},
        )
        config_path = self._codex_home / "config.toml"
        config_path.write_text(_build_mcp_toml(servers), encoding="utf-8")

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    async def run(self, prompt: str) -> str:
        """Execute ``codex exec`` and return the final assistant message.

        Returns an ``"ERROR: ..."`` string on subprocess failure or timeout
        rather than raising, so the two-phase pipeline can continue to the
        submission phase with a degraded report.
        """
        self._setup_workspace()

        output_file = self.workspace / f"{self.phase}_output.txt"
        output_file.unlink(missing_ok=True)

        # Provider-mapped credentials only; override CODEX_HOME for isolation.
        env = prepare_codex_subprocess_env(
            codex_home=self._codex_home,
            provider=self.llm_provider,
        )

        cmd = ["codex", "exec"]
        if self.reasoning_effort is not None:
            cmd += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
        cmd += [
            "-m",
            self.model,
            "-C",
            str(self.workspace),
            "--sandbox",
            "workspace-write",
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_file),
            "--json",
            prompt,
        ]

        self._logger.log(
            "subprocess_start",
            {"command": " ".join(cmd[:6] + ["..."]), "phase": self.phase},
        )

        self._reconnect_failure_at = None
        self._last_progress_at = None

        try:
            if sandbox_name_from_env():
                proc = await exec_in_sandbox(
                    cmd,
                    env=env,
                    cwd=str(self.workspace),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(self.workspace),
                )
            returncode, stderr_text = await self._stream_subprocess(proc)
        except CodexSubprocessStallError as exc:
            self._logger.log(
                "subprocess_stall",
                {
                    "phase": self.phase,
                    "stall_s": exc.stall_s,
                    "reconnect_failure": exc.reconnect_failure,
                },
            )
            return f"ERROR: {self.phase} phase {exc}"
        except asyncio.TimeoutError:
            self._logger.log(
                "subprocess_timeout", {"phase": self.phase, "timeout_s": self.timeout}
            )
            return f"ERROR: {self.phase} phase timed out after {self.timeout}s"
        except FileNotFoundError:
            self._logger.log(
                "subprocess_error", {"error": "codex binary not found in PATH"}
            )
            return "ERROR: 'codex' not found in PATH — is Codex CLI installed?"

        if returncode != 0:
            self._logger.log(
                "subprocess_error",
                {"returncode": returncode, "stderr": stderr_text[:2000]},
            )
            if self._stream_output and stderr_text.strip():
                print(stderr_text, file=sys.stderr, flush=True)
            return (
                f"ERROR: {self.phase} phase exited with code {returncode}. "
                f"stderr: {stderr_text[:400]}"
            )

        if output_file.exists():
            result = output_file.read_text(encoding="utf-8").strip()
            self._logger.log(
                "subprocess_done", {"phase": self.phase, "output_length": len(result)}
            )
            return result

        self._logger.log("subprocess_error", {"error": "output file not created"})
        return f"ERROR: {self.phase} phase produced no output"

    def _remaining_before_stall(self, loop: asyncio.AbstractEventLoop) -> float:
        now = loop.time()
        limits: list[float] = []
        if self._last_progress_at is not None:
            limits.append(self.stall_timeout - (now - self._last_progress_at))
        if self._reconnect_failure_at is not None:
            limits.append(
                RECONNECT_STALL_TIMEOUT_S - (now - self._reconnect_failure_at)
            )
        if not limits:
            return float("inf")
        return min(limits)

    def _raise_if_stalled(self, loop: asyncio.AbstractEventLoop) -> None:
        now = loop.time()
        if (
            self._reconnect_failure_at is not None
            and now - self._reconnect_failure_at > RECONNECT_STALL_TIMEOUT_S
        ):
            raise CodexSubprocessStallError(
                stall_s=RECONNECT_STALL_TIMEOUT_S,
                reconnect_failure=True,
            )
        if (
            self._last_progress_at is not None
            and now - self._last_progress_at > self.stall_timeout
        ):
            raise CodexSubprocessStallError(
                stall_s=self.stall_timeout,
                reconnect_failure=False,
            )

    def _track_codex_progress(
        self, event: dict, loop: asyncio.AbstractEventLoop
    ) -> None:
        if _reconnect_transport_failed(event):
            if self._reconnect_failure_at is None:
                self._reconnect_failure_at = loop.time()
        if _is_productive_codex_event(event):
            self._last_progress_at = loop.time()
            self._reconnect_failure_at = None

    async def _stream_subprocess(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[int, str]:
        """Read Codex stdout line-by-line until the process exits."""
        stderr_chunks: list[bytes] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout
        self._last_progress_at = loop.time()

        async def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(4096)
                if not chunk:
                    break
                stderr_chunks.append(chunk)

        stderr_task = asyncio.create_task(_read_stderr())

        try:
            assert proc.stdout is not None
            while True:
                self._raise_if_stalled(loop)

                hard_remaining = deadline - loop.time()
                if hard_remaining <= 0:
                    proc.kill()
                    await proc.wait()
                    raise asyncio.TimeoutError

                stall_remaining = self._remaining_before_stall(loop)
                remaining = min(hard_remaining, stall_remaining)
                if remaining <= 0:
                    proc.kill()
                    await proc.wait()
                    self._raise_if_stalled(loop)
                    raise asyncio.TimeoutError

                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    try:
                        self._raise_if_stalled(loop)
                    except CodexSubprocessStallError:
                        raise
                    raise

                if not line_bytes:
                    break

                self._handle_stdout_line(
                    line_bytes.decode("utf-8", errors="replace").rstrip("\n"),
                    loop=loop,
                )
        finally:
            await stderr_task

        returncode = await proc.wait()
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return returncode, stderr_text

    def _handle_stdout_line(
        self,
        raw: str,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Parse one stdout line, log it, and optionally print a summary."""
        raw = raw.strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            if self._stream_output:
                print(raw, flush=True)
            return

        if loop is not None:
            self._track_codex_progress(event, loop)
        self._log_codex_event(event)

    def _log_codex_event(self, event: dict) -> None:
        event_type = event.get("type", "codex_event")
        self._logger.log(event_type, {"codex_event": event})

        item = event.get("item") or {}
        if item.get("type") == "mcp_tool_call":
            tool = str(item.get("tool", ""))
            if event_type == "item.started":
                arguments = item.get("arguments")
                self._logger.log(
                    "tool_start",
                    {
                        "tool": {"name": tool},
                        "input": json.dumps(arguments, ensure_ascii=False)
                        if arguments is not None
                        else "{}",
                    },
                )
            elif event_type == "item.completed":
                if item.get("error") is not None:
                    self._logger.log("tool_error", {"output": str(item.get("error"))})
                else:
                    result = item.get("result")
                    if isinstance(result, dict):
                        content = result.get("content")
                        if isinstance(content, list):
                            output = "\n".join(
                                str(block.get("text", ""))
                                for block in content
                                if isinstance(block, dict)
                                and block.get("type") == "text"
                            )
                        else:
                            output = json.dumps(result, ensure_ascii=False)
                    else:
                        output = str(result or "")
                    self._logger.log(
                        "tool_end",
                        {"output": output, "output_type": "tool_result"},
                    )

        if self._stream_output:
            display = format_codex_event(event)
            if display:
                print(display, flush=True)

    def _forward_jsonl_events(self, text: str) -> None:
        """Parse ``codex --json`` JSONL lines and forward them to messages.jsonl."""
        for raw in text.splitlines():
            self._handle_stdout_line(raw)
