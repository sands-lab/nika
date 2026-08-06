"""Unit tests for RunConfig load / merge / legacy migration mapping."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from nika.run_config.legacy import (
    detect_legacy_operational_env,
    legacy_env_to_partial_dict,
    warn_legacy_operational_env,
)
from nika.run_config.loader import load_run_config, merge_cli
from nika.run_config.schema import RunConfig
from nika.utils.agent_config import (
    resolve_agent_model,
    resolve_agent_type,
    resolve_llm_provider,
    resolve_max_steps,
)
from nika.run_config.loader import set_run_config, reset_run_config


def test_load_missing_uses_defaults(tmp_path: Path) -> None:
    cfg = load_run_config(tmp_path / "missing.yaml")
    assert cfg.agent.type == "byo.langgraph"
    assert cfg.agent.max_steps == 20


def test_load_and_merge_cli(tmp_path: Path) -> None:
    path = tmp_path / "nika.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "agent": {
                    "type": "cli.claude",
                    "provider": "deepseek",
                    "max_steps": 10,
                    "models": {"claude": "deepseek-v4-flash"},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(path)
    merged = merge_cli(cfg, max_steps=30, model="override-model")
    assert merged.agent.max_steps == 30
    assert merged.agent.model == "override-model"
    assert merged.agent.provider == "deepseek"


def test_resolve_from_run_config() -> None:
    cfg = RunConfig.model_validate(
        {
            "agent": {
                "type": "byo.langgraph",
                "provider": "deepseek",
                "max_steps": 15,
                "models": {"langgraph": "deepseek-chat"},
            }
        }
    )
    set_run_config(cfg)
    try:
        assert resolve_agent_type(None) == "byo.langgraph"
        assert resolve_llm_provider(None, agent_type="byo.langgraph") == "deepseek"
        assert resolve_max_steps(None) == 15
        assert resolve_agent_model("byo.langgraph", None) == "deepseek-chat"
        assert resolve_agent_model("byo.langgraph", "cli-model") == "cli-model"
    finally:
        reset_run_config()


def test_provider_validation_via_schema() -> None:
    with pytest.raises(ValueError, match="not supported"):
        RunConfig.model_validate(
            {"agent": {"type": "cli.claude", "provider": "openai"}}
        )


def test_legacy_env_mapping() -> None:
    partial = legacy_env_to_partial_dict(
        {
            "NIKA_AGENT_TYPE": "cli.claude",
            "NIKA_LLM_PROVIDER": "deepseek",
            "NIKA_MAX_STEPS": "12",
            "NIKA_CLAUDE_MODEL": "deepseek-v4-pro[1m]",
            "NIKA_CUSTOM_BASE_URL": "https://openrouter.ai/api/v1",
            "DEEPSEEK_API_KEY": "sk-ds",
        }
    )
    assert partial["agent"]["type"] == "cli.claude"
    assert partial["agent"]["provider"] == "deepseek"
    assert partial["agent"]["max_steps"] == 12
    assert partial["agent"]["models"]["claude"] == "deepseek-v4-pro[1m]"
    assert partial["agent"]["custom"]["base_url"] == "https://openrouter.ai/api/v1"


def test_legacy_warn_does_not_apply_values(monkeypatch, capsys) -> None:
    import nika.run_config.legacy as legacy

    legacy._warned = False
    # Isolate from the developer's real .env / process leftovers.
    monkeypatch.setenv("NIKA_AGENT_TYPE", "cli.claude")
    for key in (
        "NIKA_LLM_PROVIDER",
        "NIKA_MAX_STEPS",
        "NIKA_CLAUDE_MODEL",
        "NIKA_RESULT_DIR",
        "NIKA_JUDGE_PROVIDER",
        "NIKA_JUDGE_MODEL",
        "NIKA_CUSTOM_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    warn_legacy_operational_env({"NIKA_AGENT_TYPE": "cli.claude"})
    captured = capsys.readouterr()
    assert "nika config migrate" in captured.out
    assert detect_legacy_operational_env({"NIKA_AGENT_TYPE": "cli.claude"}) == [
        "NIKA_AGENT_TYPE"
    ]


def test_legacy_warn_ignores_yaml_bridge_keys(monkeypatch, capsys) -> None:
    import nika.run_config.legacy as legacy

    legacy._warned = False
    warn_legacy_operational_env(
        {
            "NIKA_CUSTOM_BASE_URL": "https://openrouter.ai/api/v1",
            "CUSTOM_API_BASE": "https://openrouter.ai/api/v1",
        }
    )
    captured = capsys.readouterr()
    assert "WARNING" not in captured.out
