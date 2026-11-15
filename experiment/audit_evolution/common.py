"""Paths, run order, gateway settings, and the bwrap sandbox for the audit agent."""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

ROOT = Path(os.environ.get("AUDIT_EVO_ROOT", "/workspace/nika-audit-evolution"))
DATA = ROOT / "data"
SANDBOX = ROOT / "sandbox"
TREE = SANDBOX / "tree"  # 0.2.0 release code + audit library, empty KB
REPO = SANDBOX / "repo"  # git objects of the recorded commits only
PYTHON = TREE / ".venv" / "bin" / "python"
SKILL = TREE / ".agents" / "skills" / "nika-submission-audit"
EMPTY_KB = TREE / "src" / "nika" / "audit" / "kb_data"

GATEWAY = os.environ.get("KAUST_GATEWAY_URL", "http://127.0.0.1:18766")
TOKEN_FILE = Path.home() / ".config" / "kaust-gateway" / "client.token"
OPENAI_URL = "https://api.openai.com/v1"
OPENAI_KEY_FILE = Path.home() / ".config" / "nika-audit" / "openai.key"

# Runs, results and caches of different auditor models stay apart.
MODEL_TAG = os.environ.get("AUDIT_EVO_MODEL", "qwen3.8")
RUNS = ROOT / "runs" / MODEL_TAG
RESULTS = ROOT / "results" / MODEL_TAG
CACHE = ROOT / "cache" / MODEL_TAG


def benchmark_dir(name: str) -> Path:
    """Trajectory packages of one benchmark, one directory per evaluation run."""
    return DATA / name


def runs(benchmark: str) -> list[str]:
    return sorted(p.name for p in benchmark_dir(benchmark).iterdir() if p.is_dir())


def order(benchmark: str, seed: int) -> list[str]:
    """Seed 0 is date order; other seeds shuffle the runs."""
    names = runs(benchmark)
    if seed:
        random.Random(seed).shuffle(names)
    return names


def token() -> str:
    return TOKEN_FILE.read_text().strip()


def llm_env(alias: str) -> dict[str, str]:
    """Environment for the pipeline's chat calls and the audit agent.

    ``openai/<model>`` uses the OpenAI API; any other alias is a gateway alias,
    also used by Claude Code through the gateway's Messages endpoint.
    """
    if alias.startswith("openai/"):
        key = OPENAI_KEY_FILE.read_text().strip()
        return {
            "NIKA_AUDIT_LLM_URL": OPENAI_URL,
            "NIKA_AUDIT_LLM_MODEL": alias.removeprefix("openai/"),
            "NIKA_AUDIT_LLM_KEY": key,
            "OPENAI_API_KEY": key,
            "AUDIT_EVO_MODEL": MODEL_TAG,
        }
    t = token()
    return {
        "NIKA_AUDIT_LLM_URL": f"{GATEWAY}/v1",
        "NIKA_AUDIT_LLM_MODEL": alias,
        "NIKA_AUDIT_LLM_KEY": t,
        "AUDIT_EVO_MODEL": MODEL_TAG,
        "OPENAI_API_KEY": t,
        "ANTHROPIC_BASE_URL": GATEWAY,
        "ANTHROPIC_AUTH_TOKEN": t,
        "ANTHROPIC_MODEL": alias,
        "ANTHROPIC_SMALL_FAST_MODEL": alias,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
    }


def bwrap(readonly: list[Path], writable: list[Path], env: dict[str, str]) -> list[str]:
    """Command prefix that hides /home, /workspace and /tmp except the listed paths.

    The audit agent must not read the production knowledge base, the ground truth,
    or the user's Claude configuration and memory.
    """
    home = Path.home()
    cmd = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/home",
        "--tmpfs",
        "/workspace",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        "/srv",
        "--unshare-pid",
        "--die-with-parent",
        "--clearenv",
    ]
    for p in [
        home / ".local" / "share" / "uv",
        home / ".local" / "share" / "claude",
        home / ".local" / "bin",
        *readonly,
    ]:
        cmd += ["--ro-bind", str(p.resolve()), str(p)]
    for p in writable:
        cmd += ["--bind", str(p), str(p)]
    base = {
        "HOME": str(home),
        "PATH": f"{PYTHON.parent}:{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
    }
    for k, v in {**base, **env}.items():
        cmd += ["--setenv", k, v]
    return cmd


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


GATEWAY_CLI = (
    Path.home()
    / ".claude"
    / "skills"
    / "kaust-compute-gateway"
    / "scripts"
    / "gateway.py"
)
ALIASES = [
    "rocs/Qwen3.8-27B-FP8-2x80g-sxm",
    "rocs/Qwen3.8-27B-FP8-2x80g-sxm-spot",
    "rocs/Qwen3.8-27B-FP8-4x40g-spot",
]


def deployments() -> list[dict]:
    import subprocess

    out = subprocess.run(
        ["python3", str(GATEWAY_CLI), "deployments"], capture_output=True, text=True
    )
    data = json.loads(out.stdout or "{}")
    return data.get("deployments") or data.get("data") or []


def ready_replicas() -> dict[str, int]:
    """Ready replicas per experiment alias (alias = rocs/<profile>)."""
    counts = dict.fromkeys(ALIASES, 0)
    for d in deployments():
        alias = f"rocs/{d.get('profile')}"
        if alias in counts and d.get("state") == "READY":
            counts[alias] += 1
    return counts
