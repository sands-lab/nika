from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.sandbox.config import SandboxConfig, resolve_sandbox_config
from agent.sandbox.sbx.agents import (
    native_sbx_agent,
    required_sbx_template_images,
    sbx_template_image,
)
from agent.sandbox.sbx.auth import PROXY_MANAGED_SENTINEL, apply_codex_auth
from agent.sandbox.sbx.credentials import (
    ensure_sbx_credentials,
    missing_credential_message,
    required_services_for_agent,
)
from agent.sandbox.sbx.client import (
    ensure_sbx_daemon,
    require_sbx_authenticated,
    run_sbx,
    run_sbx_checked,
)
from agent.sandbox.sbx.exec import build_sbx_exec_command, exec_in_sandbox
from agent.sandbox.sbx.manager import SbxSandboxManager
from agent.sandbox.sbx.policy import _LLM_NETWORK_HOSTS
from agent.sandbox.sbx.proxy import (
    ensure_sbx_proxy_config,
    resolve_sbx_upstream_proxy,
    sbx_daemon_running,
    sbx_process_env,
)
from agent.sandbox.sbx.workspace import collect_artifacts, prepare_workspace


@pytest.mark.parametrize(
    ("agent_type", "image"),
    [
        ("cli.codex", "docker/sandbox-templates:codex-docker"),
        ("cli.claude", "docker/sandbox-templates:claude-code-docker"),
        ("sdk.codex_sdk", "docker/sandbox-templates:shell-docker"),
        ("sdk.claude_sdk", "docker/sandbox-templates:shell-docker"),
        ("community.sade", "docker/sandbox-templates:shell-docker"),
    ],
)
def test_required_sbx_template_images(agent_type: str, image: str) -> None:
    assert required_sbx_template_images(agent_type) == [image]
    assert required_sbx_template_images("byo.langgraph", agent_type) == [image]
    assert sbx_template_image(native_sbx_agent(agent_type)) == image


@pytest.mark.parametrize(
    ("agent_type", "sbx_agent"),
    [
        ("cli.codex", "codex"),
        ("cli.claude", "claude"),
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


def test_llm_policy_allows_supported_provider_endpoints() -> None:
    assert {"api.anthropic.com", "api.deepseek.com", "openrouter.ai"} <= set(
        _LLM_NETWORK_HOSTS
    )


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


def test_prepare_claude_preserves_deepseek_placeholder_for_sbx_exec(
    monkeypatch,
) -> None:
    """DeepSeek remap must not overwrite sbx-cs placeholders before sbx exec."""
    from agent.cli.claude.config import prepare_claude_subprocess_env
    from agent.sandbox.sbx.agents import ENV_SBX_SANDBOX_NAME

    monkeypatch.setenv(ENV_SBX_SANDBOX_NAME, "nika-test-sbx")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-real-deepseek")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sbx-cs-placeholder")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sbx-cs-placeholder")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic")

    env = prepare_claude_subprocess_env(provider="deepseek")
    assert env["ANTHROPIC_API_KEY"] == "sbx-cs-placeholder"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sbx-cs-placeholder"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"
    assert (
        "DEEPSEEK_API_KEY" not in env
        or env.get("DEEPSEEK_API_KEY") != "sk-real-deepseek"
    )

    command = build_sbx_exec_command(
        "nika-test",
        ["claude", "-p", "hi"],
        env=env,
    )
    inner = command[-1]
    assert "ANTHROPIC_API_KEY=sbx-cs-placeholder" in inner
    assert "ANTHROPIC_AUTH_TOKEN=sbx-cs-placeholder" in inner
    assert "sk-real-deepseek" not in inner


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


def test_sdk_bundle_imports_without_nika_package(tmp_path, monkeypatch) -> None:
    """Bundled agent tree must import and construct SDK agents without nika."""
    import json
    import subprocess
    import sys

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
    workspace = tmp_path
    (workspace / "sandbox_manifest.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "scenario_name": "simple_bgp",
                "backend": "kathara",
                "submission_context": {"fault_ontology": [], "resources": []},
                "mcp_servers": {
                    "task_mcp_server": {
                        "transport": "http",
                        "url": "http://example/mcp",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    # Simulate microVM: only workspace on path (agent package), no src/nika.
    script = (
        "import os, sys\n"
        f"sys.path.insert(0, {str(workspace)!r})\n"
        "os.environ['NIKA_SANDBOX_EXECUTION']='1'\n"
        f"os.environ['NIKA_SESSION_DIR']={str(workspace)!r}\n"
        # Block accidental nika imports from the host install.
        "sys.modules['nika'] = None\n"
        "from agent.registry import create_agent\n"
        "from agent.cli.claude import config as claude_config\n"
        "from agent.sdk.codex_sdk import config as codex_config\n"
        "assert hasattr(claude_config, 'prepare_claude_subprocess_env')\n"
        "assert hasattr(codex_config, 'codex_sdk_local_auth_available')\n"
        "for agent_type in ('sdk.codex_sdk', 'sdk.claude_sdk', 'community.sade'):\n"
        "    create_agent(\n"
        "        agent_type,\n"
        "        session_id='s1',\n"
        "        model='deepseek-v4-flash',\n"
        "        llm_provider='deepseek',\n"
        "    )\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(workspace),
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_workspace_roundtrip_keeps_only_standard_artifacts(tmp_path) -> None:
    from agent.sandbox.sbx.workspace import cleanup_workspace

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "ground_truth.json").write_text("secret", encoding="utf-8")
    (session_dir / "run.json").write_text("{}", encoding="utf-8")
    workspace = prepare_workspace(
        session_dir=session_dir,
        manifest={"session_id": "sess-1", "task_description": "diagnose"},
        runtime_env={"NIKA_AGENT_TYPE": "cli.codex"},
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


def test_open_session_collects_artifacts_when_policy_cleanup_fails(tmp_path) -> None:
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session = SimpleNamespace(
        session_id="sess-cleanup-failure",
        session_dir=str(session_dir),
        task_description="diagnose",
        scenario_name="simple_bgp",
        backend="kathara",
    )
    credentials = SimpleNamespace(sentinel_runtime_env=lambda: {})
    manager = SbxSandboxManager(resolve_sandbox_config(keep_container=False))

    with (
        patch("agent.sandbox.sbx.manager.ensure_sbx_proxy_config"),
        patch("agent.sandbox.sbx.manager.ensure_sbx_ready"),
        patch("agent.sandbox.sbx.manager.require_sbx_authenticated"),
        patch("agent.sandbox.sbx.manager.ensure_llm_network_policy"),
        patch(
            "agent.sandbox.sbx.manager.ensure_sbx_credentials",
            return_value=credentials,
        ),
        patch("agent.sandbox.sbx.manager.run_sbx_checked"),
        patch("agent.sandbox.sbx.manager.run_sbx_optional"),
        patch("agent.sandbox.sbx.manager.allow_mcp_gateway"),
        patch(
            "agent.sandbox.sbx.manager.deny_mcp_gateway",
            side_effect=OSError("policy cleanup failed"),
        ),
        patch("agent.sandbox.sbx.manager.log_event"),
    ):
        with pytest.raises(OSError, match="policy cleanup failed"):
            with manager.open_session(
                session=session,
                agent_type="cli.codex",
                model="gpt-5-mini",
                max_steps=10,
                reasoning_effort=None,
                llm_provider="openai",
                mcp_gateway_agent_url="http://host.docker.internal:12345",
                gateway_port=12345,
                stream_output=False,
            ) as sbx_session:
                (sbx_session.workspace_dir / "messages.jsonl").write_text(
                    "message\n", encoding="utf-8"
                )
                (sbx_session.workspace_dir / "submission.json").write_text(
                    "{}", encoding="utf-8"
                )

    assert (session_dir / "sandbox_manifest.json").is_file()
    assert (session_dir / "messages.jsonl").read_text(encoding="utf-8") == "message\n"
    assert (session_dir / "submission.json").is_file()
    assert not (session_dir / ".sandbox_run").exists()


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
            provider="openai",
            agent_type="cli.codex",
        )

    run.assert_called_once()
    assert plan.openai_api_key_mode
    assert plan.sentinel_runtime_env()["OPENAI_API_KEY"] == PROXY_MANAGED_SENTINEL


def test_ensure_sbx_credentials_skips_existing_custom_secret(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=tok\n",
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
            provider="deepseek",
            agent_type="cli.claude",
        )

    run.assert_not_called()
    assert plan.third_party_anthropic
    assert plan.sentinel_runtime_env()["ANTHROPIC_API_KEY"] == "sbx-cs-anth"
    assert "DEEPSEEK_API_KEY" not in plan.sentinel_runtime_env()


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
            provider="openai",
            agent_type="cli.codex",
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
            provider="anthropic",
            agent_type="cli.claude",
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
            provider="openai",
            agent_type="cli.codex",
        )
    assert "OPENAI_API_KEY" in missing_credential_message("openai")
    assert "/login" in missing_credential_message("anthropic")


def test_required_services_for_agent() -> None:
    assert required_services_for_agent("cli.codex") == frozenset({"openai"})
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
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig

    set_run_config(
        RunConfig.model_validate(
            {"nika": {"sandbox": {"upstream_proxy": "http://proxy.test:8080"}}}
        )
    )
    try:
        assert resolve_sbx_upstream_proxy() == "http://proxy.test:8080"
    finally:
        reset_run_config()


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
        patch("agent.sandbox.sbx.proxy.sbx_daemon_running", return_value=False),
        patch("agent.sandbox.sbx.proxy.logger.warning") as warning,
    ):
        run.return_value = type(
            "proc", (), {"returncode": 0, "stdout": "", "stderr": ""}
        )()
        ensure_sbx_proxy_config("http://proxy.test:8080")
    popen.assert_called_once()
    warning.assert_called_once()
    stop_cmds = [
        call.args[0]
        for call in run.call_args_list
        if call.args and call.args[0][:3] == ["sbx", "daemon", "stop"]
    ]
    assert stop_cmds == []


def test_ensure_sbx_proxy_config_does_not_stop_running_daemon(
    tmp_path, monkeypatch
) -> None:
    import agent.sandbox.sbx.proxy as proxy_mod

    monkeypatch.setattr(proxy_mod, "_proxy_lock_path", lambda: tmp_path / "lock")
    monkeypatch.setattr(proxy_mod, "_applied_proxy", None)
    with (
        patch("agent.sandbox.sbx.proxy.sbx_available", return_value=True),
        patch("agent.sandbox.sbx.proxy.sbx_daemon_running", return_value=True),
        patch("agent.sandbox.sbx.proxy._daemon_proxy_matches", return_value=False),
        patch("agent.sandbox.sbx.proxy.subprocess.run") as run,
        patch("agent.sandbox.sbx.proxy.subprocess.Popen") as popen,
        patch("agent.sandbox.sbx.proxy.logger.warning") as warning,
    ):
        ensure_sbx_proxy_config("http://proxy.test:8080")
    popen.assert_not_called()
    assert not any(
        call.args and call.args[0][:3] == ["sbx", "daemon", "stop"]
        for call in run.call_args_list
    )
    warning.assert_called()


def test_sbx_process_env_sets_https_proxy_when_unset() -> None:
    env = sbx_process_env(upstream_proxy="http://proxy.test:8080")
    assert env["HTTPS_PROXY"] == "http://proxy.test:8080"
    assert env["HTTP_PROXY"] == "http://proxy.test:8080"
    assert env["DOCKER_SANDBOXES_PROXY"] == "http://proxy.test:8080"
    assert "host.docker.internal" in env["NO_PROXY"]


def test_sbx_process_env_keeps_existing_https_proxy() -> None:
    with patch.dict(
        os.environ,
        {
            "HTTPS_PROXY": "http://already:9",
        },
        clear=True,
    ):
        env = sbx_process_env(upstream_proxy="http://proxy.test:8080")
    assert env["HTTPS_PROXY"] == "http://already:9"
    assert env["DOCKER_SANDBOXES_PROXY"] == "http://proxy.test:8080"
    assert "HTTP_PROXY" not in env or env.get("HTTP_PROXY") != "http://proxy.test:8080"


def test_run_sbx_passes_host_proxy_env() -> None:
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig

    set_run_config(
        RunConfig.model_validate(
            {"nika": {"sandbox": {"upstream_proxy": "http://proxy.test:8080"}}}
        )
    )
    try:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("agent.sandbox.sbx.client.sbx_available", return_value=True),
            patch("agent.sandbox.sbx.client.subprocess.run") as run,
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            run_sbx(["ls"], check=False)
        env = run.call_args.kwargs["env"]
        assert env["HTTPS_PROXY"] == "http://proxy.test:8080"
        assert env["DOCKER_SANDBOXES_PROXY"] == "http://proxy.test:8080"
    finally:
        reset_run_config()


def test_exec_in_sandbox_passes_host_proxy_env() -> None:
    import asyncio

    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig

    set_run_config(
        RunConfig.model_validate(
            {"nika": {"sandbox": {"upstream_proxy": "http://proxy.test:8080"}}}
        )
    )

    async def _run() -> dict[str, str]:
        with (
            patch.dict(
                os.environ,
                {
                    "NIKA_SBX_SANDBOX_NAME": "nika-test",
                },
                clear=True,
            ),
            patch("agent.sandbox.sbx.exec.asyncio.create_subprocess_exec") as create,
        ):
            create.return_value = SimpleNamespace()
            await exec_in_sandbox(["claude", "-p", "hi"], sandbox_name="nika-test")
        return create.call_args.kwargs["env"]

    try:
        env = asyncio.run(_run())
        assert env["HTTPS_PROXY"] == "http://proxy.test:8080"
        assert env["DOCKER_SANDBOXES_PROXY"] == "http://proxy.test:8080"
    finally:
        reset_run_config()


def test_ensure_sbx_daemon_uses_status_not_ls() -> None:
    with (
        patch(
            "agent.sandbox.sbx.proxy.sbx_daemon_running", return_value=True
        ) as status,
        patch("agent.sandbox.sbx.client.subprocess.run") as run,
    ):
        ensure_sbx_daemon()
    status.assert_called()
    run.assert_not_called()


def test_sbx_daemon_running_uses_live_unix_socket(tmp_path, monkeypatch) -> None:
    socket_path = tmp_path / ".local/state/sandboxes/sandboxes/sandboxd/sandboxd.sock"
    socket_path.parent.mkdir(parents=True)
    socket_path.touch()
    probe = SimpleNamespace(
        settimeout=lambda _timeout: None,
        connect=lambda path: path == str(socket_path),
        close=lambda: None,
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with (
        patch("agent.sandbox.sbx.proxy.socket.socket", return_value=probe),
        patch("agent.sandbox.sbx.proxy.subprocess.run") as run,
    ):
        assert sbx_daemon_running() is True
    run.assert_not_called()


def test_require_sbx_authenticated_fails_with_actionable_message() -> None:
    with (
        patch("agent.sandbox.sbx.client.sbx_authenticated", return_value=False),
        pytest.raises(RuntimeError, match=r"sbx login.*upstream proxy"),
    ):
        require_sbx_authenticated()


def test_run_sbx_checked_retries_hub_token_error() -> None:
    fail = SimpleNamespace(
        returncode=1,
        stdout="",
        stderr=(
            "ERROR: token is unverifiable: error while executing keyfunc: "
            'Get "https://login.docker.com/.well-known/jwks.json": '
            "context deadline exceeded\n"
        ),
    )
    ok = SimpleNamespace(returncode=0, stdout="created", stderr="")
    with (
        patch("agent.sandbox.sbx.client.sbx_available", return_value=True),
        patch("agent.sandbox.sbx.client.subprocess.run", side_effect=[fail, ok]) as run,
        patch("agent.sandbox.sbx.client.time.sleep") as sleep,
        patch.dict(os.environ, {}, clear=True),
    ):
        result = run_sbx_checked(["create", "--name", "nika-test", "claude", "/tmp"])
    assert result.returncode == 0
    assert run.call_count == 2
    sleep.assert_called_once_with(2.0)
