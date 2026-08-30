"""Unit tests for RunConfig load / merge / legacy migration mapping."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from typer.testing import CliRunner

from nika.cli.main import app
from nika.run_config.legacy import (
    detect_legacy_operational_env,
    warn_legacy_operational_env,
)
from nika.run_config.loader import (
    load_run_config,
    merge_cli,
    reset_run_config,
    set_run_config,
)
from nika.run_config.schema import RunConfig
from nika.utils.agent_config import (
    resolve_agent_model,
    resolve_agent_type,
    resolve_llm_provider,
    resolve_max_steps,
)

_RUNNER = CliRunner()


def test_load_missing_uses_defaults(tmp_path: Path) -> None:
    cfg = load_run_config(tmp_path / "missing.yaml")
    assert cfg.agent.type == "byo.langgraph"
    assert cfg.agent.max_steps == 20
    assert cfg.benchmark.case_timeout_sec == 2400


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
