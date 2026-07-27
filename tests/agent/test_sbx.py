from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.sandbox.config import SandboxConfig, resolve_sandbox_config
from agent.sandbox.sbx.agents import native_sbx_agent
from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL, apply_codex_auth
from agent.sandbox.sbx.credentials import (
    ensure_sbx_credentials,
    missing_credential_message,
    required_services_for_agent,
)
from agent.sandbox.sbx.exec import build_sbx_exec_command
from agent.sandbox.sbx.manager import SbxSandboxManager
from agent.sandbox.sbx.proxy import ensure_sbx_proxy_config, resolve_sbx_upstream_proxy
from agent.sandbox.sbx.wheels import (
    SDK_WHEEL_DIRNAME,
    install_sdk_wheels_in_sandbox,
    sdk_wheel_dir,
    stage_sdk_wheels,
)
from agent.sandbox.sbx.workspace import collect_artifacts, prepare_workspace


@pytest.mark.parametrize(
    ("agent_type", "sbx_agent"),
    [
        ("local_cli.codex_cli", "codex"),
        ("local_cli.claude_cli", "claude"),
        ("sdk.codex_sdk", "shell"),
        ("sdk.claude_sdk", "shell"),
        ("community.sade", "shell"),
    ],
)
def test_agent_mapping_and_create_command(agent_type: str, sbx_agent: str) -> None:
    manager = SbxSandboxManager(resolve_sandbox_config())

    assert native_sbx_agent(agent_type) == sbx_agent
    assert manager.build_create_command(
        sandbox_name="nika-test",
        sbx_agent=sbx_agent,
        workspace_dir=Path("/tmp/ws"),
        agent_type=agent_type,
    ) == ["create", "--name", "nika-test", sbx_agent, "/tmp/ws"]


def test_exec_command_uses_sandbox_relative_paths() -> None:
    with patch.dict(os.environ, {"NIKA_SESSION_DIR": "/tmp/sandbox"}, clear=False):
        command = build_sbx_exec_command(
            "nika-test",
            ["codex", "exec", "-m", "gpt-5.4-mini"],
            cwd="/tmp/sandbox/codex_workspace",
            env={"CODEX_HOME": "/tmp/sandbox/codex_workspace/.codex_home"},
        )

    assert command[:4] == ["sbx", "exec", "-d", "nika-test"]
    assert "CODEX_HOME=.codex_home" in command[-1]
    assert "cd codex_workspace" in command[-1]


def test_exec_command_rewrites_secret_env_to_sentinel() -> None:
    command = build_sbx_exec_command(
        "nika-test",
        ["claude", "-p", "hi"],
        env={
            "ANTHROPIC_API_KEY": "sk-real-secret",
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
        },
    )
    inner = command[-1]
    assert "sk-real-secret" not in inner
    assert f"ANTHROPIC_API_KEY={PROXY_MANAGED_SENTINEL}" in inner
    assert "ANTHROPIC_BASE_URL=https://api.anthropic.com" in inner


def test_exec_command_omits_third_party_anthropic_secrets() -> None:
    command = build_sbx_exec_command(
        "nika-test",
        ["claude", "-p", "hi"],
        env={
            "ANTHROPIC_API_KEY": "sk-real-secret",
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        },
    )
    inner = command[-1]
    assert "sk-real-secret" not in inner
    assert "ANTHROPIC_API_KEY=" not in inner
    assert "ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic" in inner


def test_exec_command_forwards_custom_placeholder() -> None:
    command = build_sbx_exec_command(
        "nika-test",
        ["claude", "-p", "hi"],
        env={
            "ANTHROPIC_API_KEY": "sbx-cs-placeholder",
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
        },
    )
    inner = command[-1]
    assert "ANTHROPIC_API_KEY=sbx-cs-placeholder" in inner


def test_workspace_roundtrip_keeps_only_standard_artifacts(tmp_path) -> None:
    from agent.sandbox.sbx.workspace import cleanup_workspace

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "ground_truth.json").write_text("secret", encoding="utf-8")
    (session_dir / "run.json").write_text("{}", encoding="utf-8")
    workspace = prepare_workspace(
        session_dir=session_dir,
        manifest={"session_id": "sess-1", "task_description": "diagnose"},
        runtime_env={"NIKA_AGENT_TYPE": "local_cli.codex_cli"},
    )

    assert not (workspace.workspace_dir / "ground_truth.json").exists()
    assert (workspace.workspace_dir / "run.json").is_file()
    assert json.loads(workspace.manifest_path.read_text())["session_id"] == "sess-1"

    (workspace.workspace_dir / "messages.jsonl").write_text("line\n", encoding="utf-8")
    (workspace.workspace_dir / "submission.json").write_text("{}", encoding="utf-8")
    sdk_workspace = workspace.workspace_dir / "codex_sdk_workspace"
    sdk_workspace.mkdir()
    (sdk_workspace / "keep.txt").write_text("ok", encoding="utf-8")
    collect_artifacts(workspace)
    cleanup_workspace(workspace)

    assert (session_dir / "messages.jsonl").read_text() == "line\n"
    assert (session_dir / "submission.json").read_text() == "{}"
    assert (session_dir / "sandbox_manifest.json").is_file()
    assert not (session_dir / ".sandbox_run").exists()
    assert not (session_dir / "codex_sdk_workspace").exists()


def test_ensure_sbx_credentials_sets_openai_for_codex(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-openai\n", encoding="utf-8")
    with (
        patch("agent.sandbox.sbx.credentials.sbx_available", return_value=True),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_secret_services",
            return_value=set(),
        ),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_custom_secrets",
            return_value={},
        ),
        patch("agent.sandbox.sbx.credentials.run_sbx_checked") as run,
        patch.dict(os.environ, {}, clear=True),
    ):
        plan = ensure_sbx_credentials(
            env_file=env_file,
            required_services={"openai"},
        )

    run.assert_called_once()
    assert plan.openai_api_key_mode
    assert plan.sentinel_runtime_env()["OPENAI_API_KEY"] == PROXY_MANAGED_SENTINEL


def test_ensure_sbx_credentials_skips_existing_custom_secret(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=tok\nANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic\n",
        encoding="utf-8",
    )
    with (
        patch("agent.sandbox.sbx.credentials.sbx_available", return_value=True),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_secret_services",
            return_value=set(),
        ),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_custom_secrets",
            return_value={"ANTHROPIC_API_KEY": "sbx-cs-anth"},
        ),
        patch("agent.sandbox.sbx.credentials.run_sbx_checked") as run,
        patch.dict(os.environ, {}, clear=True),
    ):
        plan = ensure_sbx_credentials(
            env_file=env_file,
            required_services={"anthropic"},
        )

    run.assert_not_called()
    assert plan.third_party_anthropic
    assert plan.sentinel_runtime_env()["ANTHROPIC_API_KEY"] == "sbx-cs-anth"


def test_ensure_sbx_credentials_accepts_existing_oauth_secret(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    with (
        patch("agent.sandbox.sbx.credentials.sbx_available", return_value=True),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_secret_services",
            return_value={"openai"},
        ),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_custom_secrets",
            return_value={},
        ),
        patch("agent.sandbox.sbx.credentials.run_sbx_checked") as run,
        patch.dict(os.environ, {}, clear=True),
    ):
        plan = ensure_sbx_credentials(
            env_file=env_file,
            required_services={"openai"},
        )

    run.assert_not_called()
    assert plan.openai_api_key_mode is False
    assert plan.sentinel_runtime_env() == {}


def test_ensure_sbx_credentials_accepts_existing_anthropic_oauth_secret(
    tmp_path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    with (
        patch("agent.sandbox.sbx.credentials.sbx_available", return_value=True),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_secret_services",
            return_value={"anthropic"},
        ),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_custom_secrets",
            return_value={},
        ),
        patch("agent.sandbox.sbx.credentials.run_sbx_checked") as run,
        patch.dict(os.environ, {}, clear=True),
    ):
        plan = ensure_sbx_credentials(
            env_file=env_file,
            required_services={"anthropic"},
        )

    run.assert_not_called()
    assert "anthropic" in plan.services
    assert not plan.anthropic_api_key_mode


def test_anthropic_subscription_mode_detects_sbx_secret_only() -> None:
    from agent.sandbox.sbx.credentials import anthropic_subscription_mode

    with (
        patch("agent.sandbox.sbx.credentials.sbx_available", return_value=True),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_secret_services",
            return_value=["anthropic"],
        ),
        patch.dict(os.environ, {}, clear=True),
    ):
        assert anthropic_subscription_mode() is True


def test_anthropic_subscription_mode_false_when_env_api_key_present() -> None:
    from agent.sandbox.sbx.credentials import anthropic_subscription_mode

    with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=True):
        assert anthropic_subscription_mode() is False


def test_ensure_sbx_credentials_missing_raises_guidance(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    with (
        patch("agent.sandbox.sbx.credentials.sbx_available", return_value=True),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_secret_services",
            return_value=set(),
        ),
        patch(
            "agent.sandbox.sbx.credentials.list_sbx_custom_secrets",
            return_value={},
        ),
        patch.dict(os.environ, {}, clear=True),
        pytest.raises(RuntimeError, match="openai --oauth"),
    ):
        ensure_sbx_credentials(
            env_file=env_file,
            required_services={"openai"},
        )
    assert "OPENAI_API_KEY" in missing_credential_message("openai")
    assert "/login" in missing_credential_message("anthropic")


def test_required_services_for_agent() -> None:
    assert required_services_for_agent("local_cli.codex_cli") == frozenset({"openai"})
    assert required_services_for_agent("sdk.claude_sdk") == frozenset({"anthropic"})


def test_apply_codex_auth_sandbox_api_key_mode(tmp_path) -> None:
    codex_home = tmp_path / ".codex_home"
    with patch.dict(
        os.environ,
        {
            "NIKA_SANDBOX_EXECUTION": "1",
            "OPENAI_API_KEY": PROXY_MANAGED_SENTINEL,
        },
        clear=True,
    ):
        apply_codex_auth(codex_home)
    data = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert data["OPENAI_API_KEY"] == PROXY_MANAGED_SENTINEL
    assert data["auth_mode"] == "apikey"


def test_apply_codex_auth_sandbox_subscription_skips_auth_file(tmp_path) -> None:
    codex_home = tmp_path / ".codex_home"
    with patch.dict(os.environ, {"NIKA_SANDBOX_EXECUTION": "1"}, clear=True):
        apply_codex_auth(codex_home)
    assert not (codex_home / "auth.json").exists()


def test_explicit_proxy_is_forwarded_to_sbx() -> None:
    with patch.dict(
        os.environ,
        {"NIKA_SANDBOX_UPSTREAM_PROXY": "http://proxy.test:8080"},
        clear=True,
    ):
        assert resolve_sbx_upstream_proxy() == "http://proxy.test:8080"


def test_proxy_from_main_env_file(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("NIKA_SANDBOX_UPSTREAM_PROXY=http://proxy.test:8080\n")
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_sbx_upstream_proxy(env_file=env_file) == "http://proxy.test:8080"


def test_ensure_sbx_proxy_config_no_op_without_upstream() -> None:
    with (
        patch("agent.sandbox.sbx.proxy.sbx_available", return_value=True),
        patch("agent.sandbox.sbx.proxy.subprocess.run") as run,
        patch("agent.sandbox.sbx.proxy.subprocess.Popen") as popen,
    ):
        ensure_sbx_proxy_config(None)
    run.assert_not_called()
    popen.assert_not_called()


def test_ensure_sbx_proxy_config_warns_when_daemon_stays_down() -> None:
    with (
        patch("agent.sandbox.sbx.proxy.sbx_available", return_value=True),
        patch("agent.sandbox.sbx.proxy.subprocess.run") as run,
        patch("agent.sandbox.sbx.proxy.subprocess.Popen") as popen,
        patch("agent.sandbox.sbx.proxy.time.sleep"),
        patch("agent.sandbox.sbx.proxy._daemon_running", return_value=False),
        patch("agent.sandbox.sbx.proxy.logger.warning") as warning,
    ):
        run.return_value = type(
            "proc", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )()
        ensure_sbx_proxy_config("http://proxy.test:8080")
    popen.assert_called_once()
    warning.assert_called_once()


def test_sdk_wheels_are_staged_and_installed_offline(tmp_path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with (
        patch("agent.sandbox.sbx.wheels._WHEEL_CACHE", cache),
        patch("agent.sandbox.sbx.wheels._pip_download"),
    ):
        staged = stage_sdk_wheels(workspace)
    assert staged == workspace / SDK_WHEEL_DIRNAME

    wheels = sdk_wheel_dir(workspace)
    with patch("agent.sandbox.sbx.wheels.run_sbx_checked") as run:
        install_sdk_wheels_in_sandbox(
            sandbox_name="nika-test",
            workspace_dir=workspace,
        )

    command = run.call_args.args[0]
    assert command[:3] == ["exec", "-d", "nika-test"]
    assert "--no-index" in command[-1]
    assert str(wheels) in command[-1]
    assert "-r '" in command[-1]
    assert (workspace / "requirements-sdk.txt").is_file()


def test_sdk_packages_install_from_pypi_when_offline_disabled(tmp_path) -> None:
    from agent.sandbox.sbx.wheels import install_sdk_packages_in_sandbox

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("agent.sandbox.sbx.wheels.run_sbx_checked") as run:
        install_sdk_packages_in_sandbox(
            sandbox_name="nika-test",
            workspace_dir=workspace,
            offline=False,
        )

    command = run.call_args.args[0]
    assert command[:3] == ["exec", "-d", "nika-test"]
    assert "--no-index" not in command[-1]
    assert "pip3 install" in command[-1]
    assert "-r '" in command[-1]
    assert (workspace / "requirements-sdk.txt").is_file()


def test_sdk_requirements_are_exact_pins() -> None:
    from agent.sandbox.sbx.wheels import SDK_PIP_PACKAGES, SDK_REQUIREMENTS_FILE

    assert SDK_REQUIREMENTS_FILE.is_file()
    assert SDK_PIP_PACKAGES
    assert all("==" in req for req in SDK_PIP_PACKAGES)
    assert any(req.startswith("pydantic==") for req in SDK_PIP_PACKAGES)
    assert any(req.startswith("claude-agent-sdk==") for req in SDK_PIP_PACKAGES)
    assert any(req.startswith("openai-codex==") for req in SDK_PIP_PACKAGES)


def test_resolve_sandbox_config_offline_sdk_wheels_default_off(monkeypatch) -> None:
    monkeypatch.delenv("NIKA_SANDBOX_OFFLINE_SDK_WHEELS", raising=False)
    assert resolve_sandbox_config().offline_sdk_wheels is False

    monkeypatch.setenv("NIKA_SANDBOX_OFFLINE_SDK_WHEELS", "true")
    assert resolve_sandbox_config().offline_sdk_wheels is True
    assert resolve_sandbox_config(offline_sdk_wheels=False).offline_sdk_wheels is False


def test_sdk_source_bundle_does_not_copy_nika(tmp_path) -> None:
    manager = SbxSandboxManager(
        SandboxConfig(
            env_file=tmp_path / ".env",
            keep_container=False,
            cpus=None,
            memory=None,
            offline_sdk_wheels=False,
        )
    )

    manager._bundle_agent_sources(tmp_path)

    assert (tmp_path / "agent").is_dir()
    assert not (tmp_path / "nika").exists()
    assert not any((tmp_path / "agent").rglob("nika"))
