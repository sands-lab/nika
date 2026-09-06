"""Unit tests for RunConfig load / merge / legacy migration mapping."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from typer.testing import CliRunner

from nika.cli.main import app
from nika.config import REPO_ROOT
from nika.run_config.legacy import (
    detect_legacy_operational_env,
    warn_legacy_operational_env,
)
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

_RUNNER = CliRunner()


def test_load_missing_uses_defaults(tmp_path: Path) -> None:
    cfg = load_run_config(tmp_path / "missing.yaml")
    assert cfg.agent.type == "byo.langgraph"
    assert cfg.agent.max_steps == 20
    assert cfg.benchmark.case_timeout_sec == 2400


def test_resolve_run_config_path_relative_and_absolute(tmp_path: Path) -> None:
    abs_path = (tmp_path / "nika.yaml").resolve()
    assert resolve_run_config_path(abs_path) == abs_path
    assert resolve_run_config_path("config/nika.yaml") == (
        REPO_ROOT / "config" / "nika.yaml"
    ).resolve()


def test_resolve_run_config_path_blank_and_home(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(ENV_RUN_CONFIG, raising=False)
    default = (REPO_ROOT / DEFAULT_RUN_CONFIG_REL).resolve()
    assert resolve_run_config_path("") == default
    assert resolve_run_config_path("   ") == default

    home_cfg = tmp_path / "home-nika.yaml"
    home_cfg.write_text("version: 1\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert resolve_run_config_path("~/home-nika.yaml") == home_cfg.resolve()


def test_export_run_config_env_sets_absolute(monkeypatch, tmp_path: Path) -> None:
    target = tmp_path / "custom.yaml"
    target.write_text("version: 1\n", encoding="utf-8")
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


def test_runtime_validation_rejects_invalid_depth() -> None:
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {"nika": {"runtime_validation": {"depth": "medium"}}}
        )


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


def test_merge_cli_base_url_overrides_yaml(tmp_path: Path) -> None:
    path = tmp_path / "nika.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
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
        yaml.safe_dump({"version": 1, "agent": {"type": "byo.langgraph"}}),
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
                "version": 1,
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
    assert cfg.version == 1
    assert cfg.nika.result_dir == "results"
    assert cfg.agent.type == "byo.langgraph"
    assert cfg.agent.provider == "openai"
    assert cfg.agent.model is None


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
    assert data["agent"]["models"]["claude"] == "deepseek-v4-pro[1m]"
    assert data["agent"]["custom"]["base_url"] == "https://openrouter.ai/api/v1"


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


def test_legacy_env_maps_lab_mcp_llm_knobs() -> None:
    from nika.run_config.legacy import legacy_env_to_partial_dict

    partial = legacy_env_to_partial_dict(
        {
            "NIKA_LLM_TIMEOUT": "120",
            "NIKA_LLM_RETRIES": "4",
            "NIKA_MCP_READ_TIMEOUT": "90",
            "NIKA_MCP_GATEWAY_HOST": "0.0.0.0",
            "NIKA_MCP_GATEWAY_PORT": "8080",
            "NIKA_K8S_ACCESS": "kubectl_only",
            "NIKA_K8S_APISERVER": "https://127.0.0.1:6443",
            "NIKA_DEPLOY_ATTEMPTS": "5",
            "NIKA_LAB_VERIFY_MAX_WAIT": "240",
            "NIKA_VERIFY_MAX_ATTEMPTS": "7",
        }
    )
    assert partial["agent"]["llm"]["timeout_sec"] == 120.0
    assert partial["agent"]["llm"]["max_retries"] == 4
    assert partial["nika"]["mcp"]["read_timeout_sec"] == 90.0
    assert partial["nika"]["mcp"]["gateway_host"] == "0.0.0.0"
    assert partial["nika"]["mcp"]["gateway_port"] == 8080
    assert partial["nika"]["k8s"]["access"] == "kubectl_only"
    assert partial["nika"]["k8s"]["apiserver"] == "https://127.0.0.1:6443"
    assert partial["nika"]["lab"]["deploy_attempts"] == 5
    assert partial["nika"]["lab"]["ready_max_wait_sec"] == 240.0
    assert partial["nika"]["lab"]["failure_verify_max_attempts"] == 7
    cfg = RunConfig.model_validate(partial)
    assert cfg.agent.llm.timeout_sec == 120.0
    assert cfg.nika.lab.deploy_attempts == 5


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
