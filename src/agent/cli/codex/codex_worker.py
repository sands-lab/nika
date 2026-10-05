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
  line-by-line, logged to the host ``messages.jsonl`` (``trace_dir``) in real
  time, and pretty-printed to the terminal via
  :func:`~agent.cli.codex.codex_display.format_codex_event`.
* **Step budget** – ``codex exec`` has no turn limit and reports usage only
  once per ``codex exec`` turn, so the worker infers model responses from item
  boundaries (:class:`_ResponseCounter`), logs one ``llm_end`` per response,
  and stops Codex when a response past ``max_steps`` begins.
"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent.cli.codex.codex_display import format_codex_event
from agent.protocols import PHASES
from agent.sandbox.sbx.auth import apply_codex_auth
from agent.sandbox.sbx.exec import exec_in_sandbox
from agent.utils.loggers import (
    MessageLogger,
    PendingToolCallTracker,
    tool_event_payload,
)
from agent.utils.mcp_client import load_session_mcp_config
from agent.utils.provider_env import build_agent_subprocess_env, require_provider
from agent.utils.skills import prepare_codex_workspace
from agent.utils.two_phase import max_steps_report
from agent.utils.usage import normalize_usage

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
    env = build_agent_subprocess_env(
        agent_type=agent_type, provider=require_provider(provider), base=base
    )
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


class CodexFatalError(Exception):
    """Raised when Codex reports a non-retryable API/config failure."""


class CodexMaxStepsReached(Exception):
    """Raised when Codex starts a model response past ``max_steps``."""


# Items that run a tool; their results go back to the model in a new response.
_TOOL_ITEM_TYPES = frozenset(
    {"mcp_tool_call", "command_execution", "file_change", "web_search"}
)


class _ResponseCounter:
    """Infer model responses (LLM turns) from ``codex exec --json`` items.

    A response starts with the first item after all tool calls of the previous
    response finished. Tool calls that one response issues and Codex runs one
    after another therefore count as separate responses; parallel calls count
    once. This matches the other agents' unit (one model response per step)
    as closely as the ``codex exec`` event stream allows.
    """

    def __init__(self) -> None:
        self.responses = 0
        self._pending_tools = 0
        self._awaiting_response = True

    def observe(self, event: dict) -> bool:
        """Update state for *event*; return True when it starts a new response."""
        event_type = event.get("type", "")
        if event_type not in {"item.started", "item.completed"}:
            return False
        item_type = (event.get("item") or {}).get("type")
        if item_type in {None, "error"}:
            return False
        started = False
        if self._awaiting_response:
            self._awaiting_response = False
            self.responses += 1
            started = True
        if item_type in _TOOL_ITEM_TYPES:
            if event_type == "item.started":
                self._pending_tools += 1
            else:
                self._pending_tools = max(self._pending_tools - 1, 0)
                if self._pending_tools == 0:
                    self._awaiting_response = True
        return started


def _fatal_codex_error_message(event: dict) -> str | None:
    """Return the message when the event is a permanent API/config failure.

    Transient reconnect/timeout noise is ignored so the existing stall timer
    can still handle transport flakes.  Model-metadata fallback warnings are
    also ignored — they are not fatal by themselves.
    """
    event_type = event.get("type", "")
    if event_type == "error":
        message = str(event.get("message") or "")
    elif event_type == "turn.failed":
        error = event.get("error") or {}
        message = str(error.get("message") or event.get("message") or "")
    elif event_type == "item.completed":
        item = event.get("item") or {}
        message = str(item.get("message") or "") if item.get("type") == "error" else ""
    else:
        message = ""
    if not message:
        return None
    lower = message.lower()
    if "defaulting to fallback metadata" in lower:
        return None
    if "does not exist or you do not have access" in lower:
        return message
    if "404" in lower and "not found" in lower and "model" in lower:
        return message
    if "invalid api key" in lower or "incorrect api key" in lower:
        return message
    if "401" in lower or "unauthorized" in lower:
        return message
    if "403" in lower and ("forbidden" in lower or "access" in lower):
        return message
    return None


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


def _codex_model_provider_id(provider: str | None) -> str | None:
    """Return a non-reserved Codex provider id for custom/DeepSeek endpoints.

    Built-in ids ``openai`` / ``ollama`` / ``lmstudio`` are reserved; Codex also
    ignores ``OPENAI_BASE_URL`` for third-party hosts, so NIKA must register a
    ``model_providers.*`` block and set ``model_provider`` to this id.
    """
    if not provider or not str(provider).strip():
        return None
    match str(provider).strip().lower():
        case "custom":
            return "nika_custom"
        case "deepseek":
            return "nika_deepseek"
        case _:
            return None


def _codex_provider_base_url(provider: str | None) -> str:
    """Resolve the Responses API base URL for a Codex custom/DeepSeek provider."""
    from agent.utils.provider_env import (
        DEEPSEEK_OPENAI_BASE_URL,
        ENV_OPENAI_BASE_URL,
        resolve_custom_base_url,
    )

    prov = (provider or "").strip().lower()
    env_base = os.environ.get(ENV_OPENAI_BASE_URL, "").strip()
    if prov == "deepseek":
        return env_base or DEEPSEEK_OPENAI_BASE_URL
    if prov == "custom":
        return env_base or resolve_custom_base_url()
    return env_base


def _build_mcp_toml(
    servers: dict,
    *,
    provider: str | None = None,
    base_url: str | None = None,
) -> str:
    """Serialise Codex ``config.toml``: approvals, optional provider, MCP servers."""
    lines: list[str] = [
        'approval_policy = "never"',
        'sandbox_mode = "workspace-write"',
    ]
    provider_id = _codex_model_provider_id(provider)
    resolved_base = (base_url or "").strip() or _codex_provider_base_url(provider)
    if provider_id and resolved_base:
        lines.append(f'model_provider = "{provider_id}"')
    lines.extend(
        [
            "",
            "[sandbox_workspace_write]",
            "network_access = true",
            "",
        ]
    )
    # Custom / local models often ignore Responses ``namespace`` tools and emit
    # bare nested names (``exec_shell``). Without this feature Codex
    # returns ``unsupported call: <short name>`` and never hits MCP.
    # Keep this off for OpenAI-hosted models that already emit namespace calls.
    if (provider or "").strip().lower() == "custom":
        lines.extend(
            [
                "[features]",
                "non_prefixed_mcp_tool_names = true",
                "",
            ]
        )
    if provider_id and resolved_base:
        # Codex requires Responses wire_api; chat is rejected on current builds.
        lines.extend(
            [
                f"[model_providers.{provider_id}]",
                f'name = "{provider_id}"',
                f'base_url = "{resolved_base}"',
                'env_key = "OPENAI_API_KEY"',
                'wire_api = "responses"',
                "",
            ]
        )
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
    stall_timeout:
        Kill the subprocess when no productive Codex events arrive for this
        many seconds (default 300 s).  After reconnect exhaustion the limit
        drops to :data:`RECONNECT_STALL_TIMEOUT_S`.  The wall-clock budget is
        ``agent.timeout_sec``, applied to the whole agent run by
        :func:`~agent.registry.run_agent`.
    llm_provider:
        Active LLM provider for credential mapping.
    scenario_name:
        Used by :func:`~agent.utils.mcp_servers.select_diagnosis_servers` to pick relevant servers.
        Ignored for the submission phase (which always uses the task server).
    max_steps:
        LLM-turn budget for this phase (see :class:`_ResponseCounter`).
    trace_dir:
        Host directory for ``messages.jsonl`` (default: *session_dir*).
    """

    def __init__(
        self,
        session_id: str,
        session_dir: str,
        phase: str,
        model: str = "gpt-5.4-mini",
        reasoning_effort: str | None = None,
        stall_timeout: int = DEFAULT_STALL_TIMEOUT_S,
        scenario_name: str = "",
        max_steps: int = 20,
        *,
        llm_provider: str,
        stream_output: bool = True,
        trace_dir: str | None = None,
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
        self.stall_timeout = stall_timeout
        self.scenario_name = scenario_name
        self.max_steps = max_steps
        self._reconnect_failure_at: float | None = None
        self._last_progress_at: float | None = None

        self.session_dir = Path(session_dir)
        self.workspace = self.session_dir / "codex_workspace"
        self._codex_home = self.workspace / ".codex_home"
        self._logger = MessageLogger(phase=phase, session_dir=trace_dir or session_dir)
        self._stream_output = stream_output
        self._agent_message_texts: list[str] = []
        self._pending_tool_calls = PendingToolCallTracker()
        self._responses = _ResponseCounter()
        self._response_open = False
        self._response_text: list[str] = []
        self._response_reasoning: list[str] = []

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
        config_path = self._codex_home / "config.toml"
        config_path.write_text(
            _build_mcp_toml(servers, provider=self.llm_provider),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    async def run(self, prompt: str) -> str:
        """Execute ``codex exec`` and return the final assistant message.

        Returns an ``"ERROR: ..."`` string on subprocess failure, fatal API
        error, stall, or timeout.  The two-phase agent treats diagnosis
        ``ERROR:`` results as hard failures and skips submission.
        """
        self._setup_workspace()

        output_file = self.workspace / f"{self.phase}_output.txt"
        output_file.unlink(missing_ok=True)
        self._agent_message_texts = []
        self._responses = _ResponseCounter()
        self._response_open = False
        self._response_text = []
        self._response_reasoning = []

        # Provider-mapped credentials only; override CODEX_HOME for isolation.
        env = prepare_codex_subprocess_env(
            codex_home=self._codex_home,
            provider=self.llm_provider,
        )

        cmd = ["codex", "exec"]
        provider_id = _codex_model_provider_id(self.llm_provider)
        if provider_id and _codex_provider_base_url(self.llm_provider):
            cmd += ["-c", f"model_provider={provider_id}"]
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

        self._logger.log("prompt", {"text": prompt})
        self._logger.log(
            "subprocess_start",
            {"command": " ".join(cmd[:6] + ["..."]), "phase": self.phase},
        )

        self._reconnect_failure_at = None
        self._last_progress_at = None

        try:
            proc = await exec_in_sandbox(cmd, env=env, cwd=str(self.workspace))
            returncode, stderr_text = await self._stream_subprocess(proc)
        except CodexMaxStepsReached:
            self._flush_response()
            return max_steps_report(
                self._logger,
                max_steps=self.max_steps,
                latest_text=next(
                    (t for t in reversed(self._agent_message_texts) if t.strip()), ""
                ),
            )
        except CodexFatalError as exc:
            self._logger.log(
                "subprocess_fatal",
                {"phase": self.phase, "error": str(exc)},
            )
            return f"ERROR: {self.phase} phase {exc}"
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
        except FileNotFoundError:
            self._logger.log(
                "subprocess_error", {"error": "sbx binary not found in PATH"}
            )
            return "ERROR: 'sbx' not found in PATH — is Docker Sandboxes installed?"
        # A response still open (e.g. no turn.completed on failure) keeps its step.
        self._flush_response()

        if returncode != 0:
            recovered = self._resolved_phase_output(output_file)
            if recovered:
                self._logger.log(
                    "subprocess_nonzero_recovered",
                    {
                        "phase": self.phase,
                        "returncode": returncode,
                        "output_length": len(recovered),
                        "stderr": stderr_text[:500],
                    },
                )
                if self._stream_output and stderr_text.strip():
                    print(stderr_text, file=sys.stderr, flush=True)
                return recovered
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

        result = self._resolved_phase_output(output_file)
        if result:
            self._logger.log(
                "subprocess_done", {"phase": self.phase, "output_length": len(result)}
            )
            return result

        self._logger.log("subprocess_error", {"error": "output file not created"})
        return f"ERROR: {self.phase} phase produced no output"

    def _resolved_phase_output(self, output_file: Path) -> str:
        """Prefer Codex ``--output-last-message``, else last streamed agent_message."""
        if output_file.exists():
            text = output_file.read_text(encoding="utf-8").strip()
            if text:
                return text
        for text in reversed(self._agent_message_texts):
            if text.strip():
                return text.strip()
        return ""

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
        fatal = _fatal_codex_error_message(event)
        if fatal is not None:
            raise CodexFatalError(fatal)
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

                remaining = self._remaining_before_stall(loop)
                read_timeout = None if remaining == float("inf") else max(remaining, 0)
                try:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(), timeout=read_timeout
                    )
                except TimeoutError:
                    proc.kill()
                    await proc.wait()
                    self._raise_if_stalled(loop)
                    raise CodexSubprocessStallError(
                        stall_s=self.stall_timeout, reconnect_failure=False
                    ) from None
                except asyncio.CancelledError:
                    # agent.timeout_sec expired: stop codex before unwinding.
                    proc.kill()
                    await proc.wait()
                    raise

                if not line_bytes:
                    break

                try:
                    self._handle_stdout_line(
                        line_bytes.decode("utf-8", errors="replace").rstrip("\n"),
                        loop=loop,
                    )
                except (CodexFatalError, CodexMaxStepsReached):
                    proc.kill()
                    await proc.wait()
                    raise
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

    def _flush_response(self, usage: Any | None = None) -> None:
        """Write the open model response as one ``llm_end``."""
        if not self._response_open:
            return
        payload: dict[str, Any] = {
            "text": "\n".join(self._response_text),
            "usage_metadata": normalize_usage(usage) if usage is not None else {},
        }
        if self._response_reasoning:
            payload["reasoning_content"] = "\n\n".join(self._response_reasoning)
        self._logger.log("llm_end", payload)
        self._response_open = False
        self._response_text = []
        self._response_reasoning = []

    def _log_codex_event(self, event: dict) -> None:
        event_type = event.get("type", "codex_event")
        item = event.get("item") or {}
        item_type = item.get("type")

        if self._responses.observe(event):
            self._flush_response()
            if self._responses.responses > self.max_steps:
                raise CodexMaxStepsReached()
            self._response_open = True
            self._logger.log(
                "llm_start",
                {"model": {"name": self.model}, "response": self._responses.responses},
            )

        # Canonical tool_* for MCP and shell — avoid raw item.* tool mirrors.
        if item_type == "mcp_tool_call":
            self._log_mcp_tool_item(event_type, item)
        elif item_type == "command_execution":
            self._log_command_execution_item(event_type, item)
        elif event_type == "turn.completed":
            # The turn's usage covers every response of this ``codex exec`` run;
            # the last response carries it so steps and tokens both add up.
            self._flush_response(event.get("usage") or {})
        elif event_type == "turn.started":
            pass  # Each response gets its own llm_start / llm_end pair.
        else:
            self._logger.log(event_type, {"codex_event": event})
            if event_type == "item.completed" and item_type == "agent_message":
                text = str(item.get("text") or "").strip()
                if text:
                    self._agent_message_texts.append(text)
                    self._response_text.append(text)
            elif event_type == "item.completed" and item_type == "reasoning":
                text = str(item.get("text") or "").strip()
                if text:
                    self._response_reasoning.append(text)

        if self._stream_output:
            display = format_codex_event(event)
            if display:
                print(display, flush=True)

    def _log_mcp_tool_item(self, event_type: str, item: dict) -> None:
        tool = str(item.get("tool", ""))
        item_id = item.get("id")
        if event_type == "item.started":
            self._logger.log(
                "tool_start",
                self._pending_tool_calls.register(
                    name=tool,
                    input=item.get("arguments"),
                    tool_call_id=item_id,
                ),
            )
            return
        if event_type != "item.completed":
            return
        if item.get("error") is not None:
            resolved = self._pending_tool_calls.resolve(
                name=tool,
                tool_call_id=item_id,
                input=item.get("arguments"),
            )
            self._logger.log(
                "tool_error",
                tool_event_payload(
                    name=tool or resolved.get("name") or None,
                    input=resolved.get("input") or item.get("arguments"),
                    tool_call_id=item_id,
                    output=str(item.get("error")),
                ),
            )
            return
        result = item.get("result")
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                output = "\n".join(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                output = json.dumps(result, ensure_ascii=False)
        else:
            output = str(result or "")
        resolved = self._pending_tool_calls.resolve(
            name=tool,
            tool_call_id=item_id,
            input=item.get("arguments"),
        )
        self._logger.log(
            "tool_end",
            tool_event_payload(
                name=tool or resolved.get("name") or None,
                input=resolved.get("input") or item.get("arguments"),
                tool_call_id=item_id,
                output=output,
                output_type="tool_result",
            ),
        )

    def _log_command_execution_item(self, event_type: str, item: dict) -> None:
        item_id = item.get("id")
        command = item.get("command")
        tool_input = {"command": command} if command is not None else None
        if event_type == "item.started":
            self._logger.log(
                "tool_start",
                self._pending_tool_calls.register(
                    name="bash",
                    input=tool_input,
                    tool_call_id=item_id,
                ),
            )
            return
        if event_type != "item.completed":
            return
        resolved = self._pending_tool_calls.resolve(
            name="bash",
            tool_call_id=item_id,
            input=tool_input,
        )
        output = item.get("aggregated_output")
        if output is None:
            output = ""
        exit_code = item.get("exit_code")
        failed = item.get("status") == "failed" or (
            exit_code is not None and exit_code != 0
        )
        payload = tool_event_payload(
            name=resolved.get("name") or "bash",
            input=resolved.get("input") or tool_input,
            tool_call_id=item_id,
            output=str(output),
            output_type="tool_result",
        )
        if failed:
            self._logger.log(
                "tool_error",
                {**payload, "error": f"exit_code={exit_code}"},
            )
        else:
            self._logger.log("tool_end", payload)
