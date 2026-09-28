"""Sandbox entrypoint: run one pipeline phase of the configured SDK agent.

``python -m agent.sandbox.runner diagnosis`` runs the diagnosis phase and
writes the report to ``diagnosis_report.json``. The host then freezes the
report and advances the MCP gateway (the phase-advance secret never enters the
sandbox), writes ``submission_context.json``, and starts
``python -m agent.sandbox.runner submission``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from agent.registry import create_agent
from agent.sandbox.config import (
    ENV_GATEWAY_AGENT_URL,
    ENV_SANDBOX_EXECUTION,
    ENV_SESSION_DIR,
)
from agent.protocols import DIAGNOSIS, PHASES
from agent.sandbox.constants import (
    DIAGNOSIS_REPORT_FILENAME,
    MANIFEST_FILENAME,
    RUNTIME_ENV_FILENAME,
    SUBMISSION_CONTEXT_FILENAME,
)


_FORCE_RUNTIME_ENV_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    }
)


def _apply_runtime_env(workspace_dir: Path) -> None:
    runtime_path = workspace_dir / RUNTIME_ENV_FILENAME
    if not runtime_path.is_file():
        return
    runtime_env = json.loads(runtime_path.read_text(encoding="utf-8"))
    for key, value in runtime_env.items():
        if value is None:
            continue
        text = str(value)
        # Credential placeholders must overwrite any sandbox-injected sentinel
        # (e.g. proxy-managed) so DeepSeek set-custom keys are not shadowed.
        if key in _FORCE_RUNTIME_ENV_KEYS:
            os.environ[key] = text
        elif text.strip():
            os.environ.setdefault(key, text)


def main(phase: str) -> None:
    if phase not in PHASES:
        raise SystemExit(
            f"Usage: python -m agent.sandbox.runner {{{'|'.join(PHASES)}}}"
        )
    session_dir = os.environ.get(ENV_SESSION_DIR, "").strip() or os.getcwd()
    workspace_dir = Path(session_dir).resolve()

    manifest_path = workspace_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SystemExit(f"Missing sandbox manifest: {manifest_path}")

    _apply_runtime_env(workspace_dir)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    os.environ[ENV_SANDBOX_EXECUTION] = "1"
    os.environ[ENV_SESSION_DIR] = str(workspace_dir)
    os.environ.setdefault(
        ENV_GATEWAY_AGENT_URL,
        manifest.get("mcp_gateway_agent_url", ""),
    )
    os.environ.setdefault("NIKA_SESSION_ID", manifest["session_id"])
    backend = str(manifest.get("backend", "")).strip()
    if backend:
        os.environ.setdefault("NIKA_SESSION_BACKEND", backend)

    agent_type = manifest["agent_type"]
    model = manifest["model"]
    max_steps = manifest.get("max_steps")
    if max_steps is None:
        raw = os.environ.get("NIKA_MAX_STEPS", "20").strip()
        max_steps = int(raw) if raw.isdigit() else 20
    reasoning_effort = manifest.get("reasoning_effort")
    max_tokens = manifest.get("max_tokens")
    llm_provider = manifest.get("llm_provider")
    stream_output = bool(manifest.get("stream_output", True))
    task_description = manifest["task_description"]

    agent = create_agent(
        agent_type,
        session_id=manifest["session_id"],
        llm_provider=llm_provider,
        model=model,
        max_steps=max_steps,
        reasoning_effort=reasoning_effort,
        max_tokens=max_tokens,
        stream_output=stream_output,
    )
    if phase == DIAGNOSIS:
        report = asyncio.run(agent.run_diagnosis(task_description))
        (workspace_dir / DIAGNOSIS_REPORT_FILENAME).write_text(
            json.dumps({"report": report}, ensure_ascii=False), encoding="utf-8"
        )
        return
    report = json.loads(
        (workspace_dir / DIAGNOSIS_REPORT_FILENAME).read_text(encoding="utf-8")
    )["report"]
    context = json.loads(
        (workspace_dir / SUBMISSION_CONTEXT_FILENAME).read_text(encoding="utf-8")
    )
    asyncio.run(agent.run_submission(report, context))


if __name__ == "__main__":
    try:
        main(sys.argv[1] if len(sys.argv) > 1 else "")
    except Exception as exc:
        print(f"Sandbox runner failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
