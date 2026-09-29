"""Run configuration and CLI workflow contracts."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from typer.testing import CliRunner

from nika.cli.main import app
from nika.config import REPO_ROOT
from nika.run_config.loader import (
    DEFAULT_RUN_CONFIG_REL,
    ENV_RUN_CONFIG,
    export_run_config_env,
    load_run_config,
    merge_cli,
    persist_effective_run_config,
    resolve_run_config_path,
)
from nika.run_config.schema import RunConfig

pytestmark = pytest.mark.contract

_RUNNER = CliRunner()


def test_load_missing_uses_defaults(tmp_path: Path) -> None:
    cfg = load_run_config(tmp_path / "missing.yaml")
    assert cfg.agent.type == "byo.langgraph"
    assert cfg.agent.max_steps == 20
    assert cfg.agent.enable_skills is True
    assert cfg.benchmark.case_timeout_sec == 2400


def test_resolve_run_config_path_relative_and_absolute(tmp_path: Path) -> None:
    abs_path = (tmp_path / "nika.yaml").resolve()
    assert resolve_run_config_path(abs_path) == abs_path
    assert (
        resolve_run_config_path("config/nika.yaml")
        == (REPO_ROOT / "config" / "nika.yaml").resolve()
    )


def test_resolve_run_config_path_blank_and_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv(ENV_RUN_CONFIG, raising=False)
    default = (REPO_ROOT / DEFAULT_RUN_CONFIG_REL).resolve()
    assert resolve_run_config_path("") == default
    assert resolve_run_config_path("   ") == default

    home_cfg = tmp_path / "home-nika.yaml"
    home_cfg.write_text("agent: {}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_run_config_path("~/home-nika.yaml") == home_cfg.resolve()


def test_export_run_config_env_sets_absolute(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.yaml"
    target.write_text("agent: {}\n", encoding="utf-8")
    monkeypatch.delenv(ENV_RUN_CONFIG, raising=False)
    exported = export_run_config_env(target)
    assert exported == target.resolve()
    assert Path(os.environ[ENV_RUN_CONFIG]).resolve() == exported

    monkeypatch.setenv(ENV_RUN_CONFIG, "")
    exported_default = export_run_config_env("")
    assert exported_default == (REPO_ROOT / DEFAULT_RUN_CONFIG_REL).resolve()
    assert Path(os.environ[ENV_RUN_CONFIG]).resolve() == exported_default


def test_static_validation_yaml_has_only_enabled_flag() -> None:
    cfg = RunConfig.model_validate({"nika": {"static_validation": {"enabled": True}}})
    assert cfg.nika.static_validation.enabled is True
    with pytest.raises(ValidationError, match="verifiers"):
        RunConfig.model_validate(
            {"nika": {"static_validation": {"verifiers": ["batfish"]}}}
        )


def test_runtime_validation_defaults_are_light() -> None:
    cfg = RunConfig()
    assert cfg.nika.runtime_validation.depth == "light"
    assert cfg.nika.runtime_validation.failure_effect is False
    assert cfg.nika.static_validation.enabled is False


def test_lab_containerlab_max_workers_default_and_bounds() -> None:
    assert RunConfig().nika.lab.containerlab_max_workers == 2
    cfg = RunConfig.model_validate({"nika": {"lab": {"containerlab_max_workers": 1}}})
    assert cfg.nika.lab.containerlab_max_workers == 1
    with pytest.raises(ValidationError, match="must be >= 1"):
        RunConfig.model_validate({"nika": {"lab": {"containerlab_max_workers": 0}}})


def test_agent_max_tokens_default_and_bounds() -> None:
    assert RunConfig().agent.max_tokens == 8192
    assert RunConfig.model_validate({"agent": {"max_tokens": 1}}).agent.max_tokens == 1
    with pytest.raises(ValidationError, match="agent.max_tokens must be >= 1"):
        RunConfig.model_validate({"agent": {"max_tokens": 0}})


def test_runtime_validation_rejects_invalid_depth() -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate({"nika": {"runtime_validation": {"depth": "medium"}}})


def test_runtime_validation_accepts_full_and_failure_effect() -> None:
    cfg = RunConfig.model_validate(
        {
            "nika": {
                "runtime_validation": {"depth": "full", "failure_effect": True},
            }
        }
    )
    assert cfg.nika.runtime_validation.depth == "full"
    assert cfg.nika.runtime_validation.failure_effect is True


def test_load_and_merge_cli(tmp_path: Path) -> None:
    path = tmp_path / "nika.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "type": "cli.claude",
                    "provider": "deepseek",
                    "max_steps": 10,
                    "model": "deepseek-v4-flash",
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


def test_merge_cli_base_url_overrides_yaml(tmp_path: Path) -> None:
    path = tmp_path / "nika.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "provider": "custom",
                    "custom": {"base_url": "http://yaml-endpoint/v1"},
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_run_config(path)
    merged = merge_cli(cfg, base_url="http://cli-endpoint/v1")
    assert merged.agent.custom.base_url == "http://cli-endpoint/v1"
    snapshot = persist_effective_run_config(merged)
    reloaded = load_run_config(snapshot)
    assert reloaded.agent.custom.base_url == "http://cli-endpoint/v1"


def test_config_set_writes_sparse_yaml(tmp_path: Path) -> None:
    out_path = tmp_path / "nika.yaml"
    out_path.write_text(
        yaml.safe_dump({"agent": {"type": "byo.langgraph"}}),
        encoding="utf-8",
    )
    result = _RUNNER.invoke(
        app,
        [
            "config",
            "set",
            "agent.provider=custom",
            "agent.model=qwen2.5:7b",
            "agent.custom.base_url=http://localhost:11434/v1",
            "--run-config",
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    data = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert data["agent"]["type"] == "byo.langgraph"
    assert data["agent"]["provider"] == "custom"
    assert data["agent"]["model"] == "qwen2.5:7b"
    assert data["agent"]["custom"]["base_url"] == "http://localhost:11434/v1"
    loaded = load_run_config(out_path)
    assert loaded.agent.custom.base_url == "http://localhost:11434/v1"
    assert loaded.agent.model == "qwen2.5:7b"


def test_config_set_rejects_unknown_key(tmp_path: Path) -> None:
    out_path = tmp_path / "nika.yaml"
    result = _RUNNER.invoke(
        app,
        [
            "config",
            "set",
            "nika.lab.deploy_attempts=9",
            "--run-config",
            str(out_path),
        ],
    )
    assert result.exit_code != 0
    assert "Unsupported key" in result.output


def test_config_set_rejects_invalid_provider(tmp_path: Path) -> None:
    out_path = tmp_path / "nika.yaml"
    out_path.write_text(
        yaml.safe_dump(
            {
                "agent": {"type": "cli.claude", "provider": "anthropic"},
            }
        ),
        encoding="utf-8",
    )
    result = _RUNNER.invoke(
        app,
        [
            "config",
            "set",
            "agent.provider=openai",
            "--run-config",
            str(out_path),
        ],
    )
    assert result.exit_code != 0
    assert "Invalid configuration" in result.output


def test_example_yaml_loads() -> None:
    """Tracked template must parse; agent blocks may be commented (defaults apply)."""
    path = REPO_ROOT / "config" / "nika.example.yaml"
    cfg = load_run_config(path)
    assert cfg.nika.result_dir == "results"
    assert cfg.agent.type == "byo.langgraph"
    assert cfg.agent.provider == "deepseek"
    assert cfg.agent.model == "deepseek-v4-flash"


def test_provider_validation_via_schema() -> None:
    with pytest.raises(ValueError, match="not supported"):
        RunConfig.model_validate(
            {"agent": {"type": "cli.claude", "provider": "openai"}}
        )


def test_config_migrate_writes_yaml(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NIKA_AGENT_TYPE=cli.claude",
                "NIKA_LLM_PROVIDER=deepseek",
                "NIKA_MAX_STEPS=12",
                "NIKA_CLAUDE_MODEL=deepseek-v4-pro[1m]",
                "NIKA_CUSTOM_BASE_URL=https://openrouter.ai/api/v1",
                "DEEPSEEK_API_KEY=sk-ds",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "nika.yaml"
    result = _RUNNER.invoke(
        app,
        [
            "config",
            "migrate",
            "--env-file",
            str(env_file),
            "-o",
            str(out_path),
            "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    data = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert data["agent"]["type"] == "cli.claude"
    assert data["agent"]["provider"] == "deepseek"
    assert data["agent"]["max_steps"] == 12
    assert data["agent"]["model"] == "deepseek-v4-pro[1m]"
    assert data["agent"]["custom"]["base_url"] == "https://openrouter.ai/api/v1"


def test_config_migrate_custom_model_uses_existing_provider(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NIKA_CUSTOM_MODEL=custom-model\n", encoding="utf-8")
    out_path = tmp_path / "nika.yaml"
    out_path.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "provider": "custom",
                    "custom": {"base_url": "http://localhost:11434/v1"},
                }
            }
        ),
        encoding="utf-8",
    )
    result = _RUNNER.invoke(
        app,
        ["config", "migrate", "--env-file", str(env_file), "-o", str(out_path), "-y"],
    )
    assert result.exit_code == 0, result.output
    data = yaml.safe_load(out_path.read_text(encoding="utf-8"))
    assert data["agent"]["model"] == "custom-model"


def test_config_migrate_write_env_keeps_credentials_only(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "NIKA_AGENT_TYPE=byo.langgraph",
                "NIKA_LLM_PROVIDER=openai",
                "OPENAI_API_KEY=sk-test",
                "ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic",
                "LANGFUSE_PUBLIC_KEY=pk-lf",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    out_path = tmp_path / "nika.yaml"
    result = _RUNNER.invoke(
        app,
        [
            "config",
            "migrate",
            "--env-file",
            str(env_file),
            "-o",
            str(out_path),
            "--write-env",
            "-y",
        ],
    )
    assert result.exit_code == 0, result.output
    rewritten = env_file.read_text(encoding="utf-8")
    assert "OPENAI_API_KEY=sk-test" in rewritten
    assert "LANGFUSE_PUBLIC_KEY=pk-lf" in rewritten
    assert "ANTHROPIC_BASE_URL" not in rewritten
    assert "NIKA_AGENT_TYPE" not in rewritten
    assert "NIKA_LLM_PROVIDER" not in rewritten
    assert (tmp_path / ".env.bak").is_file()


@pytest.mark.parametrize(
    ("name", "yaml_text", "expected"),
    [
        (
            "langgraph-deepseek.yaml",
            """
agent:
  type: byo.langgraph
  provider: deepseek
  model: deepseek-v4-flash
  max_steps: 20
  reasoning_effort: medium
""",
            {
                "type": "byo.langgraph",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "max_steps": 20,
                "reasoning_effort": "medium",
                "base_url": None,
            },
        ),
        (
            "codex-gptmini.yaml",
            """
agent:
  type: cli.codex
  provider: openai
  model: gpt-5-mini
  reasoning_effort: medium
""",
            {
                "type": "cli.codex",
                "provider": "openai",
                "model": "gpt-5-mini",
                "max_steps": 20,
                "reasoning_effort": "medium",
                "base_url": None,
            },
        ),
        (
            "claude-haiku.yaml",
            """
agent:
  type: cli.claude
  provider: anthropic
  model: claude-haiku-4-5
""",
            {
                "type": "cli.claude",
                "provider": "anthropic",
                "model": "claude-haiku-4-5",
                "max_steps": 20,
                "reasoning_effort": None,
                "base_url": None,
            },
        ),
        (
            "claude-sdk-deepseek.yaml",
            """
agent:
  type: sdk.claude_sdk
  provider: deepseek
  model: deepseek-v4-flash
  max_steps: 20
""",
            {
                "type": "sdk.claude_sdk",
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "max_steps": 20,
                "reasoning_effort": None,
                "base_url": None,
            },
        ),
        (
            "custom-openrouter.yaml",
            """
agent:
  type: byo.langgraph
  provider: custom
  model: deepseek/deepseek-v4-flash
  max_steps: 20
  custom:
    base_url: https://openrouter.ai/api/v1
""",
            {
                "type": "byo.langgraph",
                "provider": "custom",
                "model": "deepseek/deepseek-v4-flash",
                "max_steps": 20,
                "reasoning_effort": None,
                "base_url": "https://openrouter.ai/api/v1",
            },
        ),
    ],
)
def test_profile_yaml_loads(
    tmp_path: Path, name: str, yaml_text: str, expected: dict
) -> None:
    path = tmp_path / name
    path.write_text(yaml_text.strip() + "\n", encoding="utf-8")
    cfg = load_run_config(path)
    assert cfg.agent.type == expected["type"]
    assert cfg.agent.provider == expected["provider"]
    assert cfg.agent.model == expected["model"]
    assert cfg.agent.max_steps == expected["max_steps"]
    assert cfg.agent.reasoning_effort == expected["reasoning_effort"]
    assert cfg.agent.custom.base_url == expected["base_url"]


def test_profile_cli_config_show_accepts_run_config(tmp_path: Path) -> None:
    path = tmp_path / "codex-gptmini.yaml"
    path.write_text(
        "agent:\n  type: cli.codex\n  provider: openai\n  model: gpt-5-mini\n"
        "  reasoning_effort: medium\n",
        encoding="utf-8",
    )
    result = _RUNNER.invoke(app, ["config", "show", "--run-config", str(path)])
    assert result.exit_code == 0, result.output
    assert "cli.codex" in result.output
    assert "gpt-5-mini" in result.output


def test_profile_merge_cli_base_url_and_model(tmp_path: Path) -> None:
    path = tmp_path / "custom-openrouter.yaml"
    path.write_text(
        "agent:\n  type: byo.langgraph\n  provider: custom\n"
        "  model: deepseek/deepseek-v4-flash\n  max_steps: 20\n"
        "  custom:\n    base_url: https://openrouter.ai/api/v1\n",
        encoding="utf-8",
    )
    cfg = load_run_config(path)
    merged = merge_cli(
        cfg,
        model="other/model",
        base_url="http://localhost:11434/v1",
        reasoning_effort="low",
    )
    assert merged.agent.model == "other/model"
    assert merged.agent.custom.base_url == "http://localhost:11434/v1"
    assert merged.agent.reasoning_effort == "low"
    assert merged.agent.provider == "custom"


def test_profile_config_set_on_profile(tmp_path: Path) -> None:
    path = tmp_path / "langgraph-deepseek.yaml"
    path.write_text(
        "agent:\n  type: byo.langgraph\n  provider: deepseek\n"
        "  model: deepseek-v4-flash\n  max_steps: 20\n",
        encoding="utf-8",
    )
    result = _RUNNER.invoke(
        app,
        [
            "config",
            "set",
            "agent.type=cli.codex",
            "agent.provider=openai",
            "agent.model=gpt-5-mini",
            "agent.reasoning_effort=medium",
            "--run-config",
            str(path),
        ],
    )
    assert result.exit_code == 0, result.output
    cfg = load_run_config(path)
    assert cfg.agent.type == "cli.codex"
    assert cfg.agent.provider == "openai"
    assert cfg.agent.model == "gpt-5-mini"
    assert cfg.agent.reasoning_effort == "medium"
