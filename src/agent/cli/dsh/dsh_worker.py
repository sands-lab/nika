"""DeepSeek Harness CLI subprocess adapter for diagnosis/submission phases.

Each ``DshWorker`` instance drives one ``dsh --profile headless --json``
invocation inside an isolated, per-session workspace of the agent's sandbox.
It handles:

* **Workspace creation** – ``{session_dir}/dsh_workspace/``.
* **DSH_HOME isolation** – a private ``.dsh_home/`` inside the workspace so
  the process cannot read the user's profile.
* **MCP + model config** – the headless profile ``cordis.patch.yml`` lists
  only the current phase's HTTP MCP servers and points the llm plugin at the
  sandbox model proxy.
* **Output capture** – ``--json`` JSONL events are streamed, logged to
  ``messages.jsonl``, and reduced to canonical ``llm_end`` / ``tool_*`` rows
  when the projection allows it.
* **Step budget** – model responses are counted from ``status`` /
  ``step_start`` events; a response past ``max_steps`` kills the process.
  The host model proxy remains authoritative.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent.base import max_steps_report
from agent.cli.dsh.config import (
    ENV_DEEPSEEK_API_KEY,
    dsh_env,
    map_dsh_effort,
    resolve_dsh_command,
    resolve_dsh_model,
)
from agent.llm.endpoints import ENV_OPENAI_BASE_URL
from agent.protocols import PHASES
from agent.utils.loggers import (
    MessageLogger,
    PendingToolCallTracker,
    tool_event_payload,
)
from agent.utils.mcp_client import session_mcp_config
from agent.utils.skills import prepare_skill_workspace
from agent.utils.usage import normalize_usage

MCP_CLIENT_PLUGIN = "@deepseek-ai/dsh-mcp-client"
MCP_SERVER_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
MCP_SERVER_NAME_MAX = 32
# Route name registered on ``dsh-llm-pi-ai`` for the sandbox model proxy.
PROXY_PROVIDER = "proxy"
SKILLS_DEST = Path(".agents/skills")


class DshMaxStepsReached(Exception):
    """Raised when dsh starts a model response past ``max_steps``."""


def _is_max_steps_refusal(stderr_text: str) -> bool:
    """True when the host model proxy refused a request past ``max_steps``."""
    return "LLM request budget (max_steps)" in (stderr_text or "")


def is_model_response_start(event: dict) -> bool:
    """Return True when a ``--json`` event begins a new model response.

    Headless ``status`` events with ``phase=step_start`` match one LLM call.
    The same call also emits ``turn_start``; counting both would double
    ``max_steps``.
    """
    return event.get("type") == "status" and event.get("phase") == "step_start"


def sanitize_mcp_server_name(name: str, used: set[str] | None = None) -> str:
    """Return a unique ``serverName`` matching ``[A-Za-z0-9_-]{1,32}``.

    The MCP client rejects longer names, so ``kathara_base_mcp_server`` is
    shortened by dropping a trailing ``_mcp_server`` suffix.
    """
    trimmed = re.sub(r"_mcp_server$", "", name)
    trimmed = MCP_SERVER_NAME_RE.sub("_", trimmed).strip("_") or "mcp"
    base = trimmed[:MCP_SERVER_NAME_MAX]
    if used is None:
        return base
    candidate = base
    suffix = 2
    while candidate in used:
        extra = f"_{suffix}"
        candidate = (base[: MCP_SERVER_NAME_MAX - len(extra)] + extra)[
            :MCP_SERVER_NAME_MAX
        ]
        suffix += 1
    used.add(candidate)
    return candidate


def _yaml_scalar(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _dump_yaml(value: Any, *, indent: int = 0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        if not value:
            return f"{pad}{{}}"
        lines: list[str] = []
        for key, item in value.items():
            rendered_key = str(key)
            if isinstance(item, dict | list):
                if not item:
                    empty = "{}" if isinstance(item, dict) else "[]"
                    lines.append(f"{pad}{rendered_key}: {empty}")
                else:
                    lines.append(f"{pad}{rendered_key}:")
                    lines.append(_dump_yaml(item, indent=indent + 1))
            else:
                lines.append(f"{pad}{rendered_key}: {_yaml_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                inner = _dump_yaml(item, indent=indent + 1)
                first, *rest = inner.split("\n")
                lines.append(f"{pad}- {first.lstrip()}")
                lines.extend(rest)
            elif isinstance(item, list):
                lines.append(f"{pad}-")
                lines.append(_dump_yaml(item, indent=indent + 1))
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_yaml_scalar(value)}"


def _mcp_entries(servers: dict) -> list[dict[str, Any]]:
    used: set[str] = set()
    entries: list[dict[str, Any]] = []
    for name, srv in servers.items():
        if srv.get("transport") != "http" or not srv.get("url"):
            continue
        server_name = sanitize_mcp_server_name(name, used)
        config: dict[str, Any] = {
            "serverName": server_name,
            "transport": "streamable-http",
            "url": srv["url"],
            "failOnStartupError": True,
        }
        headers = srv.get("headers") or {}
        if headers:
            config["headers"] = dict(headers)
        entries.append(
            {
                "id": f"mcp-{server_name}",
                "name": MCP_CLIENT_PLUGIN,
                "config": config,
            }
        )
    return entries


def build_headless_patch(
    servers: dict,
    *,
    model: str,
    base_url: str | None = None,
    reasoning_effort: str | None = None,
    max_tokens: int | None = None,
) -> str:
    """Serialise a headless-profile ``cordis.patch.yml`` for one phase."""
    ops: list[dict[str, Any]] = []
    mcp_rows = _mcp_entries(servers)
    if mcp_rows:
        ops.append({"insert": mcp_rows})

    effort = map_dsh_effort(reasoning_effort)
    resolved_base = (base_url or "").strip() or os.environ.get(
        ENV_OPENAI_BASE_URL, ""
    ).strip()

    # The native adapter speaks DeepSeek Messages, not the sandbox OpenAI
    # proxy. Keep its shipped identity; route the run through llm-pi-ai.
    deepseek_cfg: dict[str, Any] = {"apiKeyEnv": ENV_DEEPSEEK_API_KEY}
    if effort is not None:
        deepseek_cfg["reasoningEffort"] = effort
    ops.append({"id": "llm-deepseek", "config": deepseek_cfg})

    proxy: dict[str, Any] = {
        "apiKeyEnv": ENV_DEEPSEEK_API_KEY,
        "api": "openai-completions",
        "models": [{"id": model}],
    }
    if resolved_base:
        proxy["baseURL"] = resolved_base
    if effort is not None:
        proxy["reasoning"] = effort
    if max_tokens is not None:
        proxy["defaultMaxTokens"] = max_tokens
    ops.append({"id": "llm-pi-ai", "config": {"providers": {PROXY_PROVIDER: proxy}}})

    default_model: dict[str, Any] = {"provider": PROXY_PROVIDER, "model": model}
    if effort is not None:
        default_model["reasoningEffort"] = effort
    ops.append({"id": "agent-default-model", "config": default_model})
    ops.append({"id": "approval", "config": {"policy": "never"}})
    # Search/fetch plugins default to the public DeepSeek endpoint.
    ops.append({"id": "tool-web", "disabled": True})
    ops.append({"id": "web-search-deepseek", "disabled": True})
    ops.append({"id": "web-deepseek-api", "disabled": True})
    return _dump_yaml(ops) + "\n"


class DshWorker:
    """Run one non-interactive headless ``dsh`` invocation for a pipeline phase.

    Parameters
    ----------
    session_id:
        Opaque session handle of the run.
    session_dir:
        Root of the agent workspace (``dsh_workspace/`` is created here).
    phase:
        One of :data:`~agent.protocols.PHASES` (``diagnosis`` or ``submission``).
    model:
        Model id selected for this run (required).
    llm_provider:
        Active LLM provider.
    max_steps:
        LLM-turn budget for this phase. The wall-clock budget of the whole
        run is enforced by the host.
    scenario_name:
        Scenario of the session (informational).
    trace_dir:
        Directory for ``messages.jsonl`` (default: *session_dir*).
    max_tokens:
        Output-token cap per model response, when the adapter accepts one.
    reasoning_effort:
        Run reasoning effort, mapped to dsh thinking; omitted when unset
        or ``none``.
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
        map_dsh_effort(reasoning_effort)

        self.session_id = session_id
        self.phase = phase
        self.llm_provider = llm_provider
        self.model = resolve_dsh_model(model)
        self.max_steps = max_steps
        self.scenario_name = scenario_name
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort

        self.session_dir = Path(session_dir)
        self.workspace = self.session_dir / "dsh_workspace"
        self._dsh_home = self.workspace / ".dsh_home"
        self._patch_path = self._dsh_home / "profiles" / "headless" / "cordis.patch.yml"
        self._logger = MessageLogger(phase=phase, session_dir=trace_dir or session_dir)
        self._stream_output = stream_output
        self._pending_tool_calls = PendingToolCallTracker()
        self._responses = 0
        self._response_open = False
        self._response_text: list[str] = []
        self._response_reasoning: list[str] = []
        self._last_assistant_text = ""
        self._final_text = ""
        self._error_message = ""

    def _setup_workspace(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._dsh_home.mkdir(parents=True, exist_ok=True)
        prepare_skill_workspace(self.workspace, SKILLS_DEST)

    def _write_profile_patch(self) -> None:
        servers = session_mcp_config(self.phase)
        self._logger.log(
            "mcp_config",
            {"phase": self.phase, "servers": list(servers.keys())},
        )
        self._patch_path.parent.mkdir(parents=True, exist_ok=True)
        base_url = os.environ.get(ENV_OPENAI_BASE_URL, "").strip()
        self._patch_path.write_text(
            build_headless_patch(
                servers,
                model=self.model,
                base_url=base_url,
                reasoning_effort=self.reasoning_effort,
                max_tokens=self.max_tokens,
            ),
            encoding="utf-8",
        )

    def _ensure_headless_profile(self, *, dsh_bin: str, env: dict[str, str]) -> None:
        """Initialize the shipped headless profile on first use, if needed."""
        marker = self._dsh_home / "profiles" / "headless" / "package.json"
        if marker.is_file():
            return
        try:
            subprocess.run(
                [dsh_bin, "--profile", "headless", "--help"],
                env=env,
                cwd=str(self.workspace),
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            self._logger.log(
                "profile_init",
                {"error": str(exc), "phase": self.phase},
            )

    async def run(self, prompt: str) -> str:
        """Execute headless ``dsh`` and return the final assistant message.

        Returns an ``"ERROR: ..."`` string on subprocess failure. The
        two-phase agent treats diagnosis ``ERROR:`` results as hard failures
        and skips submission.
        """
        self._setup_workspace()
        self._responses = 0
        self._response_open = False
        self._response_text = []
        self._response_reasoning = []
        self._last_assistant_text = ""
        self._final_text = ""
        self._error_message = ""

        env = dsh_env(dsh_home=self._dsh_home, session_dir=self.session_dir)
        dsh_bin = resolve_dsh_command(self.session_dir, path=env.get("PATH"))
        if dsh_bin is None:
            self._logger.log("subprocess_error", {"error": "dsh not found in PATH"})
            return "ERROR: 'dsh' not found in PATH"

        self._ensure_headless_profile(dsh_bin=dsh_bin, env=env)
        # Write after init so a template copy cannot clobber the phase patch.
        self._write_profile_patch()

        cmd = [dsh_bin, "--profile", "headless", "--json", prompt]
        self._logger.log("prompt", {"text": prompt})
        self._logger.log(
            "subprocess_start",
            {"command": " ".join(cmd[:4] + ["..."]), "phase": self.phase},
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
        except DshMaxStepsReached:
            self._flush_response()
            return max_steps_report(
                self._logger,
                max_steps=self.max_steps,
                latest_text=self._last_assistant_text,
            )
        except FileNotFoundError:
            self._logger.log("subprocess_error", {"error": "dsh not found in PATH"})
            return "ERROR: 'dsh' not found in PATH"
        self._flush_response()

        if returncode != 0 and _is_max_steps_refusal(stderr_text):
            return max_steps_report(
                self._logger,
                max_steps=self.max_steps,
                latest_text=self._last_assistant_text,
            )

        if returncode != 0:
            recovered = (self._final_text or self._last_assistant_text).strip()
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
            detail = self._error_message or stderr_text[:400]
            self._logger.log(
                "subprocess_error",
                {"returncode": returncode, "stderr": stderr_text[:2000]},
            )
            if self._stream_output and stderr_text.strip():
                print(stderr_text, file=sys.stderr, flush=True)
            return f"ERROR: {self.phase} phase exited with code {returncode}. stderr: {detail}"

        result = (self._final_text or self._last_assistant_text).strip()
        if result:
            self._logger.log(
                "subprocess_done",
                {"phase": self.phase, "output_length": len(result)},
            )
            return result

        self._logger.log("subprocess_error", {"error": "no result event captured"})
        return f"ERROR: {self.phase} phase produced no output"

    async def _stream_subprocess(
        self, proc: asyncio.subprocess.Process
    ) -> tuple[int, str]:
        """Read dsh stdout line-by-line until the process exits."""
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
                except DshMaxStepsReached:
                    proc.kill()
                    await proc.wait()
                    raise
        finally:
            await stderr_task

        returncode = await proc.wait()
        stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return returncode, stderr_text

    def _handle_stdout_line(self, raw: str) -> None:
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
        if not isinstance(event, dict):
            return
        self._log_dsh_event(event)

    def _flush_response(self, usage: Any | None = None) -> None:
        """Write the open model response as one ``llm_end``."""
        if not self._response_open:
            return
        text = "\n".join(self._response_text)
        if text.strip():
            self._last_assistant_text = text
        payload: dict[str, Any] = {
            "text": text,
            "usage_metadata": normalize_usage(usage) if usage is not None else {},
        }
        if self._response_reasoning:
            payload["reasoning_content"] = "\n\n".join(self._response_reasoning)
        self._logger.log("llm_end", payload)
        self._response_open = False
        self._response_text = []
        self._response_reasoning = []

    def _log_dsh_event(self, event: dict) -> None:
        event_type = event.get("type", "dsh_event")

        if is_model_response_start(event):
            self._flush_response()
            self._responses += 1
            if self._responses > self.max_steps:
                raise DshMaxStepsReached()
            self._response_open = True
            self._logger.log(
                "llm_start",
                {"model": {"name": self.model}, "response": self._responses},
            )
            return

        if event_type == "tool_call":
            self._log_tool_call(event)
            return
        if event_type == "tool_result":
            self._log_tool_result(event)
            return

        if event_type == "text":
            text = str(event.get("text") or "")
            if text:
                self._response_text.append(text)
                self._last_assistant_text = "\n".join(self._response_text)
            self._logger.log(event_type, {"dsh_event": event})
            return
        if event_type == "thinking":
            text = str(event.get("text") or "")
            if text:
                self._response_reasoning.append(text)
            self._logger.log(event_type, {"dsh_event": event})
            return
        if event_type == "status" and event.get("phase") in {
            "step_end",
            "turn_end",
        }:
            self._flush_response(event.get("usage"))
            return
        if event_type == "final":
            text = str(event.get("text") or "").strip()
            if text:
                self._final_text = text
                self._last_assistant_text = text
            self._logger.log(event_type, {"dsh_event": event})
            return
        if event_type == "error":
            self._error_message = str(event.get("message") or event.get("error") or "")
            self._logger.log(event_type, {"dsh_event": event})
            return

        self._logger.log(event_type, {"dsh_event": event})

    def _log_tool_call(self, event: dict) -> None:
        tool = str(event.get("tool") or event.get("name") or "")
        call_id = event.get("callId") or event.get("call_id") or event.get("id")
        tool_input = event.get("input")
        if tool_input is None:
            tool_input = event.get("arguments")
        self._logger.log(
            "tool_start",
            self._pending_tool_calls.register(
                name=tool,
                input=tool_input,
                tool_call_id=call_id,
            ),
        )

    def _log_tool_result(self, event: dict) -> None:
        tool = str(event.get("tool") or event.get("name") or "")
        call_id = event.get("callId") or event.get("call_id") or event.get("id")
        result = event.get("result")
        if result is None:
            result = event.get("output")
        output = (
            result
            if isinstance(result, str)
            else json.dumps(result, ensure_ascii=False)
        )
        status = str(event.get("status") or "").lower()
        resolved = self._pending_tool_calls.resolve(
            name=tool,
            tool_call_id=call_id,
            input=event.get("input"),
        )
        payload = tool_event_payload(
            name=tool or resolved.get("name") or None,
            input=resolved.get("input") or event.get("input"),
            tool_call_id=call_id,
            output=output,
            output_type="tool_result",
        )
        if status in {"error", "failed"}:
            self._logger.log("tool_error", {**payload, "error": output})
        else:
            self._logger.log("tool_end", payload)
