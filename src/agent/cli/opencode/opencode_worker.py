"""OpenCode CLI subprocess adapter for diagnosis/submission phases.

Each ``OpenCodeWorker`` instance drives one ``opencode run`` invocation inside
an isolated, per-session workspace of the agent's sandbox.  It handles:

* **Workspace creation** – ``{session_dir}/opencode_workspace/`` (safe to call
  multiple times).
* **Config isolation** – ``opencode.json`` lives in that workspace; ``HOME``,
  ``XDG_CONFIG_HOME``, and ``OPENCODE_CONFIG`` point there so the process does
  not read a user login directory.
* **MCP server config** – the file contains only the current phase's HTTP
  servers (:func:`~agent.utils.mcp_client.session_mcp_config` with ``phase``).
* **Model routing** – a custom provider named ``endpoint`` that reads the
  sandbox ``OPENAI_*`` / ``ANTHROPIC_*`` variables.
* **Output capture** – JSONL events from ``--format json`` are logged to
  ``messages.jsonl`` (``trace_dir``). Canonical ``llm_end`` / ``tool_start`` /
  ``tool_end`` rows are emitted when the stream allows it.
* **Step budget** – ``opencode run`` has no turn limit, so the worker counts
  ``step_start`` events (one per model response) and stops OpenCode when a
  response past ``max_steps`` begins.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

from agent.base import max_steps_report
from agent.llm.endpoints import (
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_AUTH_TOKEN,
    ENV_ANTHROPIC_BASE_URL,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
    openai_api_key,
    openai_base_url,
)
from agent.protocols import PHASES
from agent.utils.loggers import (
    MessageLogger,
    PendingToolCallTracker,
    tool_event_payload,
)
from agent.utils.mcp_client import session_mcp_config
from agent.utils.skills import prepare_skill_workspace
from agent.utils.usage import normalize_usage

PROVIDER_ID = "endpoint"
NPM_BIN_DIRNAME = Path(".npm-prefix") / "bin"
OPENCODE_NOT_FOUND = "ERROR: 'opencode' not found in PATH"
SKILLS_DEST = Path(".opencode") / "skills"
# OpenCode's default MCP fetch timeout is 5 s; diagnosis tools need longer.
MCP_TIMEOUT_MS = 120_000
REASONING_EFFORT_LEVELS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)


class OpenCodeMaxStepsReached(Exception):
    """Raised when OpenCode starts a model response past ``max_steps``."""


def resolve_opencode_model(model: str | None) -> str:
    """Use *model* when set; otherwise raise."""
    if model and model.strip():
        return model.strip()
    raise ValueError("The run has no OpenCode model.")


def map_opencode_variant(reasoning_effort: str | None) -> str | None:
    """Map the run's reasoning_effort to OpenCode ``--variant``.

    Returns ``None`` when the flag should be omitted (unset or ``none``).
    ``xhigh`` maps to OpenCode's ``max``.
    """
    if reasoning_effort is None or reasoning_effort == "none":
        return None
    if reasoning_effort not in REASONING_EFFORT_LEVELS:
        raise ValueError(
            f"reasoning_effort must be one of {REASONING_EFFORT_LEVELS}, "
            f"got {reasoning_effort!r}"
        )
    if reasoning_effort == "xhigh":
        return "max"
    return reasoning_effort


def opencode_model_ref(model: str) -> str:
    """Return ``endpoint/<model>`` for ``opencode run --model``."""
    prefix = f"{PROVIDER_ID}/"
    if model.startswith(prefix):
        return model
    return f"{prefix}{model}"


def _is_http_server(srv: dict) -> bool:
    return srv.get("transport") == "http" or "url" in srv


def build_mcp_section(servers: dict) -> dict[str, dict[str, Any]]:
    """Remote MCP entries for ``opencode.json`` (current-phase HTTP servers)."""
    mcp: dict[str, dict[str, Any]] = {}
    for name, srv in servers.items():
        if not _is_http_server(srv):
            continue
        entry: dict[str, Any] = {
            "type": "remote",
            "url": srv["url"],
            "enabled": True,
            "timeout": MCP_TIMEOUT_MS,
            "oauth": False,
        }
        if srv.get("headers"):
            entry["headers"] = srv["headers"]
        mcp[name] = entry
    return mcp


def _anthropic_sdk_base_url(url: str) -> str:
    """Return an Anthropic SDK base URL that already includes ``/v1``."""
    text = (url or "").strip().rstrip("/")
    if not text or text.endswith("/v1"):
        return text
    return f"{text}/v1"


def build_provider_section(
    *,
    llm_provider: str,
    model: str,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Custom ``endpoint`` provider that uses the sandbox model variables."""
    provider = (llm_provider or "").strip().lower()
    prefix = f"{PROVIDER_ID}/"
    model_id = model[len(prefix) :] if model.startswith(prefix) else model
    if provider == "anthropic":
        npm = "@ai-sdk/anthropic"
        # ``@ai-sdk/anthropic`` posts to ``{baseURL}/messages``. NIKA's proxy
        # (and Anthropic-compatible gateways) serve ``/v1/messages``.
        base_url = _anthropic_sdk_base_url(
            os.environ.get(ENV_ANTHROPIC_BASE_URL, "").strip()
        )
        api_key = (
            os.environ.get(ENV_ANTHROPIC_API_KEY, "").strip()
            or os.environ.get(ENV_ANTHROPIC_AUTH_TOKEN, "").strip()
        )
        key_env = ENV_ANTHROPIC_API_KEY
    else:
        npm = "@ai-sdk/openai-compatible"
        base_url = (openai_base_url(provider) or "").strip() or os.environ.get(
            ENV_OPENAI_BASE_URL, ""
        ).strip()
        api_key = (openai_api_key(provider) or "").strip()
        key_env = ENV_OPENAI_API_KEY

    options: dict[str, Any] = {}
    if base_url:
        options["baseURL"] = base_url
    options["apiKey"] = api_key or f"{{env:{key_env}}}"

    model_cfg: dict[str, Any] = {"name": model_id}
    # OpenCode requires ``limit.context`` whenever ``limit`` is present.
    # Custom endpoints are not in models.dev, so supply a conservative window.
    if max_tokens is not None:
        model_cfg["limit"] = {
            "context": 128_000,
            "output": max_tokens,
        }

    return {
        PROVIDER_ID: {
            "npm": npm,
            "name": PROVIDER_ID,
            "options": options,
            "models": {model_id: model_cfg},
        }
    }


def build_opencode_config(
    servers: dict,
    *,
    llm_provider: str,
    model: str,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """Full ``opencode.json`` body for one phase."""
    return {
        "autoupdate": False,
        "model": opencode_model_ref(model),
        "permission": {"*": "allow"},
        "provider": build_provider_section(
            llm_provider=llm_provider, model=model, max_tokens=max_tokens
        ),
        "mcp": build_mcp_section(servers),
    }


def is_model_response_start(event: dict) -> bool:
    """True when a JSONL event starts a new model response (one ``max_steps`` unit)."""
    return event.get("type") == "step_start"


class OpenCodeWorker:
    """Run one non-interactive ``opencode run`` invocation for a pipeline phase.

    Parameters
    ----------
    session_id:
        Opaque session handle of the run.
    session_dir:
        Root of the agent workspace (``opencode_workspace/`` is created here).
    phase:
        One of :data:`~agent.protocols.PHASES` (``diagnosis`` or ``submission``).
    model:
        Model name forwarded as ``--model endpoint/<model>``.
    llm_provider:
        Active LLM provider.
    max_steps:
        LLM-turn budget for this phase. Counted from ``step_start`` events.
        The wall-clock budget of the whole run is enforced by the host.
    scenario_name:
        Scenario of the session (informational).
    trace_dir:
        Directory for ``messages.jsonl`` (default: *session_dir*).
    max_tokens:
        Output-token cap per model response.
    reasoning_effort:
        Forwarded as ``--variant``; omitted when unset or ``none``.
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
        reasoning_effort: str | None = None,
    ) -> None:
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")

        self.session_id = session_id
        self.phase = phase
        self.llm_provider = llm_provider
        self.model = resolve_opencode_model(model)
        self.max_steps = max_steps
        self.scenario_name = scenario_name
        self.max_tokens = max_tokens
        map_opencode_variant(reasoning_effort)
        self.reasoning_effort = reasoning_effort

        self.session_dir = Path(session_dir)
        self.workspace = self.session_dir / "opencode_workspace"
        self._config_path = self.workspace / "opencode.json"
        self._logger = MessageLogger(phase=phase, session_dir=trace_dir or session_dir)
        self._stream_output = stream_output
        self._pending_tool_calls = PendingToolCallTracker()
        self._started_tool_ids: set[str] = set()
        self._assistant_texts: list[str] = []
        self._response_count = 0
        self._response_open = False
        self._response_text: list[str] = []
        self._response_reasoning: list[str] = []
        self._session_error = ""

    # ------------------------------------------------------------------
    # Workspace + isolated config
    # ------------------------------------------------------------------

    def _setup_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        prepare_skill_workspace(self.workspace, SKILLS_DEST)
        self._write_config()

    def _write_config(self) -> None:
        servers = session_mcp_config(self.phase)
        self._logger.log(
            "mcp_config",
            {"phase": self.phase, "servers": list(servers.keys())},
        )
        config = build_opencode_config(
            servers,
            llm_provider=self.llm_provider,
            model=self.model,
            max_tokens=self.max_tokens,
        )
        self._config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _npm_bin_dir(self) -> Path:
        return self.session_dir / NPM_BIN_DIRNAME

    def _opencode_env(self) -> dict[str, str]:
        """Sandbox environment with OpenCode home and config isolated."""
        env = dict(os.environ)
        npm_bin = str(self._npm_bin_dir())
        env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")
        env["HOME"] = str(self.workspace)
        env["XDG_CONFIG_HOME"] = str(self.workspace / ".config")
        env["XDG_DATA_HOME"] = str(self.workspace / ".local" / "share")
        env["OPENCODE_CONFIG"] = str(self._config_path)
        env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
        if self.max_tokens is not None:
            env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] = str(self.max_tokens)
        return env

    def _build_command(self, prompt: str) -> list[str]:
        cmd = [
            "opencode",
            "run",
            "--auto",
            "--format",
            "json",
            "--model",
            opencode_model_ref(self.model),
        ]
        variant = map_opencode_variant(self.reasoning_effort)
        if variant:
            cmd += ["--variant", variant]
        cmd.append(prompt)
        return cmd

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    async def run(self, prompt: str) -> str:
        """Execute ``opencode run`` and return the final assistant message.

        Returns an ``"ERROR: ..."`` string on subprocess failure. The
        two-phase agent treats diagnosis ``ERROR:`` results as hard failures
        and skips submission.
        """
        self._setup_workspace()
        self._assistant_texts = []
        self._response_count = 0
        self._response_open = False
        self._response_text = []
        self._response_reasoning = []
        self._session_error = ""
        self._pending_tool_calls = PendingToolCallTracker()
        self._started_tool_ids = set()

        env = self._opencode_env()
        if shutil.which("opencode", path=env.get("PATH")) is None:
            self._logger.log(
                "subprocess_error", {"error": "opencode not found in PATH"}
            )
            return OPENCODE_NOT_FOUND

        cmd = self._build_command(prompt)
        self._logger.log("prompt", {"text": prompt})
        self._logger.log(
            "subprocess_start",
            {"command": " ".join(cmd[:6] + ["..."]), "phase": self.phase},
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                cwd=str(self.workspace),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=8 * 1024 * 1024,
            )
            returncode, stderr_text = await self._stream_subprocess(proc)
        except OpenCodeMaxStepsReached:
            self._flush_response()
            return max_steps_report(
                self._logger,
                max_steps=self.max_steps,
                latest_text=self._latest_assistant_text(),
            )
        except FileNotFoundError:
            self._logger.log(
                "subprocess_error", {"error": "opencode not found in PATH"}
            )
            return OPENCODE_NOT_FOUND

        self._flush_response()

        if self._session_error:
            self._logger.log(
                "subprocess_error",
                {"error": self._session_error, "stderr": stderr_text[:2000]},
            )
            return f"ERROR: {self.phase} phase {self._session_error}"

        if returncode != 0:
            recovered = self._latest_assistant_text()
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

        result = self._latest_assistant_text()
        if result:
            self._logger.log(
                "subprocess_done",
                {"phase": self.phase, "output_length": len(result)},
            )
            return result

        self._logger.log("subprocess_error", {"error": "no assistant text captured"})
        return f"ERROR: {self.phase} phase produced no output"

    def _latest_assistant_text(self) -> str:
        for text in reversed(self._assistant_texts):
            if text.strip():
                return text.strip()
        joined = "\n".join(self._response_text).strip()
        return joined

    async def _stream_subprocess(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[int, str]:
        """Read OpenCode stdout line-by-line until the process exits."""
        stderr_chunks: list[bytes] = []

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
                    proc.kill()
                    await proc.wait()
                    raise

                if not line_bytes:
                    break

                try:
                    self._handle_stdout_line(
                        line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                    )
                except OpenCodeMaxStepsReached:
                    proc.kill()
                    await proc.wait()
                    raise
        finally:
            await stderr_task

        returncode = await proc.wait()
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return returncode, stderr_text

    def _handle_stdout_line(self, raw: str) -> None:
        """Parse one stdout line, log it, and emit canonical trace events."""
        raw = raw.strip()
        if not raw:
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            if self._stream_output:
                print(raw, flush=True)
            return
        if not isinstance(event, dict):
            return
        self._observe_event(event)

    def _observe_event(self, event: dict) -> None:
        event_type = event.get("type", "")
        if is_model_response_start(event):
            self._on_step_start(event)
            return
        if event_type == "step_finish":
            self._on_step_finish(event)
            return
        if event_type == "tool_use":
            self._on_tool_use(event)
            return
        if event_type == "text":
            self._on_text(event)
            return
        if event_type == "reasoning":
            self._on_reasoning(event)
            return
        if event_type == "error":
            self._on_error(event)
            return
        self._logger.log(event_type or "opencode_event", {"opencode_event": event})

    def _on_step_start(self, event: dict) -> None:
        self._flush_response()
        self._response_count += 1
        if self._response_count > self.max_steps:
            raise OpenCodeMaxStepsReached()
        self._response_open = True
        self._logger.log(
            "llm_start",
            {
                "model": {"name": self.model},
                "response": self._response_count,
            },
        )

    def _on_step_finish(self, event: dict) -> None:
        part = event.get("part") or {}
        tokens = part.get("tokens") or {}
        usage = {
            "input_tokens": tokens.get("input"),
            "output_tokens": tokens.get("output"),
            "reasoning_tokens": tokens.get("reasoning"),
        }
        self._flush_response(usage)

    def _on_text(self, event: dict) -> None:
        part = event.get("part") or {}
        text = str(part.get("text") or "").strip()
        if not text:
            return
        self._assistant_texts.append(text)
        self._response_text.append(text)

    def _on_reasoning(self, event: dict) -> None:
        part = event.get("part") or {}
        text = str(part.get("text") or "").strip()
        if text:
            self._response_reasoning.append(text)

    def _on_error(self, event: dict) -> None:
        error = event.get("error") or {}
        if isinstance(error, str) and error.strip():
            self._session_error = error.strip()
        else:
            data = error.get("data") if isinstance(error, dict) else None
            message = ""
            if isinstance(data, dict):
                message = str(data.get("message") or "")
            if not message and isinstance(error, dict):
                message = str(error.get("message") or error.get("name") or "")
            if message:
                self._session_error = message
        if self._session_error:
            self._logger.log(
                "error",
                {"error": self._session_error, "opencode_event": event},
            )

    def _on_tool_use(self, event: dict) -> None:
        part = event.get("part") or {}
        tool = str(part.get("tool") or "")
        call_id = part.get("callID") or part.get("id")
        state = part.get("state") or {}
        tool_input = state.get("input")
        status = str(state.get("status") or "")
        output = state.get("output")
        failed = status == "error" or state.get("error") is not None
        call_key = str(call_id) if call_id else ""
        if not call_key or call_key not in self._started_tool_ids:
            self._logger.log(
                "tool_start",
                self._pending_tool_calls.register(
                    name=tool,
                    input=tool_input,
                    tool_call_id=call_id,
                ),
            )
            if call_key:
                self._started_tool_ids.add(call_key)
        in_progress = (
            not failed
            and output is None
            and status in {"", "pending", "running", "start"}
        )
        if in_progress:
            return

        resolved = self._pending_tool_calls.resolve(
            name=tool,
            tool_call_id=call_id,
            input=tool_input,
        )
        if output is None and state.get("error") is not None:
            output = str(state.get("error"))
        payload = tool_event_payload(
            name=tool or resolved.get("name") or None,
            input=resolved.get("input") or tool_input,
            tool_call_id=call_id,
            output=output if output is not None else "",
            output_type="tool_result",
        )
        if not failed:
            metadata = state.get("metadata") or {}
            exit_code = metadata.get("exit")
            if exit_code is not None and exit_code != 0:
                failed = True
                payload = {**payload, "error": f"exit_code={exit_code}"}
        if failed:
            if "error" not in payload:
                payload = {**payload, "error": str(state.get("error") or status)}
            self._logger.log("tool_error", payload)
        else:
            self._logger.log("tool_end", payload)

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
