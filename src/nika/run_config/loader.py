"""Load and merge NIKA run configuration (CLI > YAML > defaults)."""

from __future__ import annotations

import copy
import logging
import os
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import yaml

from nika.config import REPO_ROOT
from nika.run_config.schema import RunConfig, default_run_config

logger = logging.getLogger(__name__)

ENV_RUN_CONFIG = "NIKA_RUN_CONFIG"
DEFAULT_RUN_CONFIG_REL = Path("config") / "nika.yaml"

_current: ContextVar[RunConfig | None] = ContextVar("nika_run_config", default=None)
_warned_missing = False


def default_run_config_path() -> Path:
    raw = os.environ.get(ENV_RUN_CONFIG, "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()
    return (REPO_ROOT / DEFAULT_RUN_CONFIG_REL).resolve()


def load_run_config(path: str | Path | None = None) -> RunConfig:
    """Load YAML run config; missing file → code defaults."""
    global _warned_missing
    cfg_path = Path(path) if path is not None else default_run_config_path()
    if not cfg_path.is_absolute():
        cfg_path = (REPO_ROOT / cfg_path).resolve()

    if not cfg_path.is_file():
        if not _warned_missing:
            _warned_missing = True
            logger.warning(
                "Run config not found at %s; using built-in defaults. "
                "Copy config/nika.example.yaml to config/nika.yaml "
                "(use `nika config migrate` only to convert legacy .env ops).",
                cfg_path,
            )
        return default_run_config()

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Run config must be a mapping: {cfg_path}")
    return RunConfig.model_validate(data)


def get_run_config() -> RunConfig:
    """Return the active run config for this context (loads default if unset)."""
    current = _current.get()
    if current is not None:
        return current
    cfg = load_run_config()
    _current.set(cfg)
    return cfg


def set_run_config(config: RunConfig) -> None:
    _current.set(config)


def reset_run_config() -> None:
    _current.set(None)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def merge_cli(
    config: RunConfig,
    *,
    agent_type: str | None = None,
    llm_provider: str | None = None,
    model: str | None = None,
    max_steps: int | None = None,
    reasoning_effort: str | None = None,
    access_role: str | None = None,
    result_dir: str | None = None,
    judge_provider: str | None = None,
    judge_model: str | None = None,
    sandbox_keep: bool | None = None,
    sandbox_cpus: str | None = None,
    sandbox_memory: str | None = None,
    sandbox_offline_sdk_wheels: bool | None = None,
    sandbox_upstream_proxy: str | None = None,
    batch_size: int | None = None,
    case_timeout_sec: int | None = None,
    continue_on_error: bool | None = None,
    retry_passes: int | None = None,
    resume: bool | None = None,
    session_tag: str | None = None,
    release: str | None = None,
    split: str | None = None,
    enable_skills: bool | None = None,
) -> RunConfig:
    """Apply non-None CLI overrides onto *config* (CLI wins)."""
    data = config.model_dump(mode="python")
    overlay: dict[str, Any] = {}

    agent_overlay: dict[str, Any] = {}
    if agent_type is not None:
        agent_overlay["type"] = agent_type
    if llm_provider is not None:
        agent_overlay["provider"] = llm_provider
    if model is not None:
        agent_overlay["model"] = model
    if max_steps is not None:
        agent_overlay["max_steps"] = max_steps
    if reasoning_effort is not None:
        agent_overlay["reasoning_effort"] = reasoning_effort
    if access_role is not None:
        agent_overlay["access"] = {"role": access_role}
    if agent_overlay:
        overlay["agent"] = agent_overlay

    nika_overlay: dict[str, Any] = {}
    if result_dir is not None:
        nika_overlay["result_dir"] = result_dir
    if enable_skills is not None:
        nika_overlay["enable_skills"] = enable_skills
    judge_overlay: dict[str, Any] = {}
    if judge_provider is not None:
        judge_overlay["provider"] = judge_provider
    if judge_model is not None:
        judge_overlay["model"] = judge_model
    if judge_overlay:
        nika_overlay["judge"] = judge_overlay
    sandbox_overlay: dict[str, Any] = {}
    if sandbox_keep is not None:
        sandbox_overlay["keep"] = sandbox_keep
    if sandbox_cpus is not None:
        sandbox_overlay["cpus"] = sandbox_cpus
    if sandbox_memory is not None:
        sandbox_overlay["memory"] = sandbox_memory
    if sandbox_offline_sdk_wheels is not None:
        sandbox_overlay["offline_sdk_wheels"] = sandbox_offline_sdk_wheels
    if sandbox_upstream_proxy is not None:
        sandbox_overlay["upstream_proxy"] = sandbox_upstream_proxy
    if sandbox_overlay:
        nika_overlay["sandbox"] = sandbox_overlay
    if nika_overlay:
        overlay["nika"] = nika_overlay

    bench_overlay: dict[str, Any] = {}
    if batch_size is not None:
        bench_overlay["batch_size"] = batch_size
    if case_timeout_sec is not None:
        bench_overlay["case_timeout_sec"] = case_timeout_sec
    if continue_on_error is not None:
        bench_overlay["continue_on_error"] = continue_on_error
    if retry_passes is not None:
        bench_overlay["retry_passes"] = retry_passes
    if resume is not None:
        bench_overlay["resume"] = resume
    if session_tag is not None:
        bench_overlay["session_tag"] = session_tag
    if release is not None:
        bench_overlay["release"] = release
    if split is not None:
        bench_overlay["split"] = split
    if bench_overlay:
        overlay["benchmark"] = bench_overlay

    if not overlay:
        return config
    merged = _deep_merge(data, overlay)
    # If CLI changed agent type without provider, keep YAML provider only when
    # compatible; otherwise pick a valid default for the new agent.
    try:
        return RunConfig.model_validate(merged)
    except Exception:
        agent = merged.get("agent") or {}
        agent_type = str(agent.get("type") or "byo.langgraph")
        provider = str(agent.get("provider") or "openai")
        from agent.utils.provider_env import AGENT_PROVIDERS, SUPPORTED_PROVIDERS

        allowed = AGENT_PROVIDERS.get(
            agent_type.lower(), frozenset(SUPPORTED_PROVIDERS)
        )
        if provider not in allowed and allowed:
            agent["provider"] = sorted(allowed)[0]
            merged["agent"] = agent
        return RunConfig.model_validate(merged)


def dump_run_config(config: RunConfig, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        config.model_dump(mode="python"),
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )
    path.write_text(text, encoding="utf-8")
