"""Claude Code CLI subprocess adapter for diagnosis/submission phases.

Each ``ClaudeWorker`` instance drives one ``claude -p`` invocation inside an
isolated, per-session workspace.  It handles:

* **Workspace creation** – ``{session_dir}/claude_workspace/`` (safe to call
  multiple times).
* **MCP server config** – a per-phase ``{phase}_mcp_config.json`` JSON file is
  written in the workspace, containing only the servers relevant to the current
  phase and scenario (selected by
  :func:`~agent.utils.mcp_servers.select_diagnosis_servers`).
* **Session ID propagation** – ``NIKA_SESSION_ID`` is injected into every MCP
  server's ``env`` block, exactly as :class:`~agent.utils.mcp_servers.MCPServerConfig`
  does for the LangChain path.
* **Auth** – environment API key/token (``--bare``) or ``claude auth login``
  OAuth; see :mod:`agent.cli.claude.config`.
* **Output capture** – the final assistant message is extracted from the
  ``{"type":"result"}`` stream-json event; all events are logged to
  ``messages.jsonl`` and pretty-printed via
  :func:`~agent.cli.claude.claude_display.format_claude_event`.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from agent.cli.claude.claude_display import (
    format_claude_event,
    should_log_claude_event,
)
from agent.cli.claude.config import (
    prepare_claude_subprocess_env,
    resolve_claude_model,
    use_bare_claude_mode,
)
from agent.utils.loggers import MessageLogger
from agent.sandbox.sbx.exec import exec_in_sandbox, sandbox_name_from_env
from agent.utils.mcp_client import begin_submission_mcp_phase, load_session_mcp_config
from agent.protocols import PHASES, SUBMISSION
from agent.utils.skills import prepare_claude_workspace, skills_enabled

# k8s MCP tool results can emit stream-json lines well above asyncio's 64KiB default.
_STREAM_READER_LIMIT = 8 * 1024 * 1024


def _build_mcp_json(servers: dict) -> str:
    """Serialise an MCP server dict (from MCPServerConfig) as JSON."""
    mcp_servers: dict = {}
    for name, srv in servers.items():
        if srv.get("transport") == "http":
            entry: dict = {
                "type": "http",
                "url": srv["url"],
            }
            if srv.get("headers"):
                entry["headers"] = srv["headers"]
        else:
            entry = {
                "type": "stdio",
                "command": srv["command"],
                "args": srv["args"],
            }
            if srv.get("env"):
                entry["env"] = srv["env"]
        mcp_servers[name] = entry
    return json.dumps({"mcpServers": mcp_servers}, indent=2)


class ClaudeWorker:
    """Run one non-interactive ``claude -p`` invocation for a pipeline phase.

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
        Claude model name forwarded to ``claude --model``.  When omitted,
        requires ``agent.models.claude`` / ``-m`` (see
        :func:`~agent.cli.claude.config.resolve_claude_model`).
    llm_provider:
        Active LLM provider for credential mapping.
    timeout:
        Hard timeout in seconds for the subprocess (default 600 s).
    scenario_name:
        Used by :func:`~agent.utils.mcp_servers.select_diagnosis_servers` to pick
        relevant servers.  Ignored for the submission phase.
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        phase: str,
        model: str | None = None,
        timeout: int = 600,
        scenario_name: str = "",
        *,
        llm_provider: str,
        stream_output: bool = True,
    ) -> None:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")

        self.session_id = session_id
        self.phase = phase
        self.llm_provider = llm_provider
        self.model = resolve_claude_model(model)
        self.timeout = timeout
        self.scenario_name = scenario_name

        self.session_dir = Path(session_dir)
        self.workspace = self.session_dir / "claude_workspace"
        self._logger = MessageLogger(phase=phase, session_dir=session_dir)
        self._stream_output = stream_output
        self._mcp_config_path: Path | None = None

    # ------------------------------------------------------------------
    # Workspace + MCP config setup
    # ------------------------------------------------------------------

    def _setup_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        prepare_claude_workspace(self.workspace)
        self._write_mcp_config()

    def _write_mcp_config(self) -> None:
        if self.phase == SUBMISSION:
            begin_submission_mcp_phase(self.session_id)
        servers = load_session_mcp_config(
            self.session_id,
            self.scenario_name,
            session_dir=self.session_dir,
        )
        # The gateway enforces phase access, but keeping the submission server
        # out of the diagnosis config also keeps the fault catalog out of the
        # agent's visible tool inventory.
        from nika.mcp.registry import SUBMISSION_SERVER

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
        config_path = self.workspace / f"{self.phase}_mcp_config.json"
        config_path.write_text(_build_mcp_json(servers), encoding="utf-8")
        self._mcp_config_path = config_path

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    async def run(self, prompt: str) -> str:
        """Execute ``claude -p`` and return the final assistant message.

        Returns an ``"ERROR: ..."`` string on subprocess failure or timeout
        rather than raising, so the two-phase pipeline can continue to the
        submission phase with a degraded report.
        """
        self._setup_workspace()

        env = prepare_claude_subprocess_env(provider=self.llm_provider)
        # API-key / token mode needs --bare so Claude uses env credentials
        # (including set-custom placeholders) instead of prompting for /login.
        # Subscription / OAuth mode must not use --bare.
        bare = use_bare_claude_mode(provider=self.llm_provider)

        assert self._mcp_config_path is not None
        cmd = [
            "claude",
            "-p",
        ]
        if bare:
            cmd.append("--bare")
        cmd += [
            "--dangerously-skip-permissions",
            "--mcp-config",
            str(self._mcp_config_path),
            "--model",
            self.model,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        if skills_enabled():
            cmd += ["--setting-sources", "project"]
        cmd.append(prompt)

        self._logger.log(
            "subprocess_start",
            {"command": " ".join(cmd[:6] + ["..."]), "phase": self.phase},
        )

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
                    limit=_STREAM_READER_LIMIT,
                )
            returncode, final_result, stderr_text = await self._stream_subprocess(proc)
        except asyncio.TimeoutError:
            self._logger.log(
                "subprocess_timeout", {"phase": self.phase, "timeout_s": self.timeout}
            )
            return f"ERROR: {self.phase} phase timed out after {self.timeout}s"
        except FileNotFoundError:
            self._logger.log(
                "subprocess_error", {"error": "claude binary not found in PATH"}
            )
            return "ERROR: 'claude' not found in PATH — is Claude Code installed?"

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

        if final_result:
            self._logger.log(
                "subprocess_done",
                {"phase": self.phase, "output_length": len(final_result)},
            )
            return final_result

        self._logger.log("subprocess_error", {"error": "no result event captured"})
        return f"ERROR: {self.phase} phase produced no output"

    async def _stream_subprocess(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[int, str, str]:
        """Read claude stdout line-by-line until the process exits.

        Returns ``(returncode, final_result, stderr_text)``.
        The *final_result* is extracted from the ``{"type":"result"}`` event.
        """
        stderr_chunks: list[bytes] = []
        final_result = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout

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
                remaining = deadline - loop.time()
                if remaining <= 0:
                    proc.kill()
                    await proc.wait()
                    raise asyncio.TimeoutError

                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=remaining
                    )
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
                    raise

                if not line_bytes:
                    break

                raw = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                result = self._handle_stdout_line(raw)
                if result is not None:
                    final_result = result
        finally:
            await stderr_task

        returncode = await proc.wait()
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return returncode, final_result, stderr_text

    def _handle_stdout_line(self, raw: str) -> str | None:
        """Parse one stdout line, log it, and optionally print a summary.

        Returns the final result string if this line is the ``result`` event,
        otherwise ``None``.
        """
        raw = raw.strip()
        if not raw:
            return None
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            if self._stream_output:
                print(raw, flush=True)
            return None

        if should_log_claude_event(event):
            self._logger.log(
                event.get("type", "claude_event"),
                {"claude_event": event},
            )
        if self._stream_output:
            try:
                display = format_claude_event(event)
            except Exception:
                display = None
            if display:
                print(display, flush=True)

        # The result event carries the final assistant response.
        if event.get("type") == "result" and not event.get("is_error"):
            return event.get("result", "")

        return None
