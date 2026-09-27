"""Claude Code CLI subprocess adapter for diagnosis/submission phases.

Each ``ClaudeWorker`` instance drives one ``claude -p`` invocation inside an
isolated, per-session workspace.  It handles:

* **Workspace creation** – ``{session_dir}/claude_workspace/`` (safe to call
  multiple times).
* **MCP server config** – a per-phase ``{phase}_mcp_config.json`` JSON file is
  written in the workspace, containing only the current phase's servers
  (:func:`~agent.utils.mcp_client.load_session_mcp_config` with ``phase``).
* **Session ID propagation** – ``NIKA_SESSION_ID`` is injected into every MCP
  server's ``env`` block, exactly as :class:`~agent.utils.mcp_servers.MCPServerConfig`
  does for the LangChain path.
* **Auth** – environment API key/token (``--bare``) or ``claude auth login``
  OAuth; see :mod:`agent.cli.claude.config`.
* **Output capture** – the final assistant message is extracted from the
  ``{"type":"result"}`` stream-json event; all events are logged to the host
  ``messages.jsonl`` (``trace_dir``, outside the sandbox workspace) and
  pretty-printed via :func:`~agent.cli.claude.claude_display.format_claude_event`.
* **Execution** – ``claude`` always runs inside the sbx sandbox
  (:func:`~agent.sandbox.sbx.exec.exec_in_sandbox`); the worker stays on the host.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from agent.cli.claude.claude_display import (
    format_claude_event,
    should_log_claude_event,
)
from agent.cli.claude.config import (
    custom_model_claude_env,
    prepare_claude_subprocess_env,
    resolve_claude_model,
    use_bare_claude_mode,
)
from agent.cli.claude.vllm_shim import new_shim_token, vllm_messages_shim
from agent.protocols import PHASES
from agent.sandbox.config import SANDBOX_GATEWAY_HOST_BRIDGE
from agent.sandbox.sbx.exec import exec_in_sandbox, sandbox_name_from_env
from agent.sandbox.sbx.policy import allow_mcp_gateway, deny_mcp_gateway
from agent.utils.loggers import MessageLogger
from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.provider_env import resolve_custom_api_key
from agent.utils.skills import prepare_claude_workspace, skills_enabled
from agent.utils.two_phase import max_steps_report


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
        Root of the sandbox workspace (``claude_workspace/`` is created here).
    phase:
        One of :data:`~agent.protocols.PHASES` (``diagnosis`` or ``submission``).
    model:
        Claude model name forwarded to ``claude --model``.  When omitted,
        requires ``agent.model`` / ``-m`` (see
        :func:`~agent.cli.claude.config.resolve_claude_model`).
    llm_provider:
        Active LLM provider for credential mapping.
    max_steps:
        LLM-turn budget for this phase, passed as ``claude --max-turns``
        (Claude Code counts one turn per model response).
        The wall-clock budget is ``agent.timeout_sec``, applied to the whole
        agent run by :func:`~agent.registry.run_agent`.
    scenario_name:
        Used by :func:`~agent.utils.mcp_servers.select_diagnosis_servers` to pick
        relevant servers.  Ignored for the submission phase.
    trace_dir:
        Host directory for ``messages.jsonl`` (default: *session_dir*). CLI
        agents pass the host session dir so the sandbox cannot edit the trace.
    max_tokens:
        Output-token cap per model response (``CLAUDE_CODE_MAX_OUTPUT_TOKENS``).
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        phase: str,
        model: str | None = None,
        max_steps: int = 20,
        scenario_name: str = "",
        *,
        llm_provider: str,
        stream_output: bool = True,
        trace_dir: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")

        self.session_id = session_id
        self.phase = phase
        self.llm_provider = llm_provider
        self.model = resolve_claude_model(model)
        self.max_steps = max_steps
        self.scenario_name = scenario_name
        self.max_tokens = max_tokens

        self.session_dir = Path(session_dir)
        self.workspace = self.session_dir / "claude_workspace"
        self._logger = MessageLogger(phase=phase, session_dir=trace_dir or session_dir)
        self._stream_output = stream_output
        self._mcp_config_path: Path | None = None
        self._last_assistant_text = ""
        self._max_turns_reached = False

    # ------------------------------------------------------------------
    # Workspace + MCP config setup
    # ------------------------------------------------------------------

    def _setup_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        prepare_claude_workspace(self.workspace)
        self._write_mcp_config()

    def _write_mcp_config(self) -> None:
        servers = load_session_mcp_config(
            self.session_id,
            self.scenario_name,
            session_dir=self.session_dir,
            phase=self.phase,
        )
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

    @contextmanager
    def _custom_endpoint_shim(self, env: dict[str, str]) -> Iterator[None]:
        """Route a custom endpoint through the host-side vLLM compatibility shim.

        The sandbox gets a per-phase shim token as its API key; the shim checks
        it and sends the real ``NIKA_CUSTOM_API_KEY`` (if any) upstream.
        """
        upstream = env.get("ANTHROPIC_BASE_URL", "").strip()
        if self.llm_provider != "custom" or not upstream:
            yield
            return
        sandbox = sandbox_name_from_env()
        token = new_shim_token()
        # The microVM reaches the host bridge, not host loopback; the token
        # keeps other clients on that interface out.
        with vllm_messages_shim(
            upstream,
            bind_host="0.0.0.0",
            token=token,
            api_key=resolve_custom_api_key(),
        ) as port:
            env["ANTHROPIC_BASE_URL"] = f"http://{SANDBOX_GATEWAY_HOST_BRIDGE}:{port}"
            env["ANTHROPIC_API_KEY"] = token
            env["ANTHROPIC_AUTH_TOKEN"] = token
            allow_mcp_gateway(sandbox_name=sandbox, port=port)
            try:
                yield
            finally:
                deny_mcp_gateway(sandbox_name=sandbox, port=port)

    async def run(self, prompt: str) -> str:
        """Execute ``claude -p`` and return the final assistant message.

        Returns an ``"ERROR: ..."`` string on subprocess failure or timeout.
        The two-phase agent treats diagnosis ``ERROR:`` results as hard
        failures and skips submission.
        """
        self._setup_workspace()

        env = prepare_claude_subprocess_env(provider=self.llm_provider)
        if self.llm_provider == "custom":
            env.update(custom_model_claude_env(self.model))
        if self.max_tokens is not None:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_tokens)
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
            "--max-turns",
            str(self.max_steps),
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

        with self._custom_endpoint_shim(env):
            try:
                proc = await exec_in_sandbox(cmd, env=env, cwd=str(self.workspace))
                returncode, final_result, stderr_text = await self._stream_subprocess(
                    proc
                )
            except FileNotFoundError:
                self._logger.log(
                    "subprocess_error", {"error": "sbx binary not found in PATH"}
                )
                return "ERROR: 'sbx' not found in PATH — is Docker Sandboxes installed?"

        if self._max_turns_reached:
            return max_steps_report(
                self._logger,
                max_steps=self.max_steps,
                latest_text=self._last_assistant_text,
            )
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
                try:
                    line_bytes = await proc.stdout.readline()
                except asyncio.CancelledError:
                    # agent.timeout_sec expired: stop claude before unwinding.
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

        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content") or []
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            if any(texts):
                self._last_assistant_text = "\n".join(t for t in texts if t)

        # The result event carries the final assistant response.
        if event.get("type") == "result":
            if event.get("subtype") == "error_max_turns":
                self._max_turns_reached = True
                return None
            if not event.get("is_error"):
                return event.get("result", "")

        return None
