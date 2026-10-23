"""DeepSeek Harness settings for agents that run ``dsh`` in their sandbox.

The host points ``OPENAI_API_KEY`` and ``OPENAI_BASE_URL`` at the sandbox
model proxy (OpenAI-compatible). ``dsh`` reads ``DEEPSEEK_*`` for its native
adapter, so those names are aliased for the child process.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from agent.llm.endpoints import (
    ENV_DEEPSEEK_API_KEY,
    ENV_DEEPSEEK_BASE_URL,
    ENV_OPENAI_API_KEY,
    ENV_OPENAI_BASE_URL,
)

ENV_DSH_HOME = "DSH_HOME"
DSH_BIN = "dsh"

# Run-config values accepted by the worker; mapped before they reach dsh.
REASONING_EFFORT_LEVELS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
)


def resolve_dsh_model(model: str | None) -> str:
    """Use *model* when set; otherwise raise."""
    if model and model.strip():
        return model.strip()
    raise ValueError("The run has no DeepSeek Harness model.")


def map_dsh_effort(reasoning_effort: str | None) -> str | None:
    """Map the run's reasoning_effort to a dsh thinking level.

    Returns ``None`` when effort should be omitted (unset or ``none``).
    """
    if reasoning_effort is None or reasoning_effort == "none":
        return None
    if reasoning_effort not in REASONING_EFFORT_LEVELS:
        raise ValueError(
            f"reasoning_effort must be one of {REASONING_EFFORT_LEVELS}, "
            f"got {reasoning_effort!r}"
        )
    match reasoning_effort:
        case "minimal" | "low":
            return "low"
        case "medium" | "high":
            return "high"
        case "xhigh":
            return "max"
        case _:
            return None


def npm_bin_dir(session_dir: str | Path) -> Path:
    """Workspace directory that holds the staged ``dsh`` binary."""
    return Path(session_dir) / ".npm-prefix" / "bin"


def resolve_dsh_command(
    session_dir: str | Path, *, path: str | None = None
) -> str | None:
    """Return the ``dsh`` executable after prepending the workspace npm bin dir."""
    candidate = npm_bin_dir(session_dir) / DSH_BIN
    if candidate.is_file():
        return str(candidate)
    return shutil.which(DSH_BIN, path=path)


def dsh_env(*, dsh_home: str | Path, session_dir: str | Path) -> dict[str, str]:
    """Sandbox environment with an isolated home, npm PATH, and credential aliases."""
    env = dict(os.environ)
    env[ENV_DSH_HOME] = str(dsh_home)
    # Keep one-shot runs off the public telemetry collector.
    env["DSH_TELEMETRY_DISABLED"] = "1"
    # Headless jobs have no TTY for approval prompts.
    env.setdefault("DSH_PERMISSION_MODE", "danger-full-access")
    npm_bin = str(npm_bin_dir(session_dir))
    env["PATH"] = npm_bin + os.pathsep + env.get("PATH", "")
    api_key = os.environ.get(ENV_OPENAI_API_KEY, "").strip()
    if api_key:
        env[ENV_DEEPSEEK_API_KEY] = api_key
    base_url = os.environ.get(ENV_OPENAI_BASE_URL, "").strip()
    if base_url:
        env[ENV_DEEPSEEK_BASE_URL] = base_url
    return env
