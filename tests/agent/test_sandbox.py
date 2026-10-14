from __future__ import annotations

import pytest

import json
import os
from unittest.mock import patch

from agent.sandbox.env import format_env_for_log
from agent.sandbox.mcp_manifest import build_sandbox_mcp_servers
from agent.sandbox.redact import redact_text
from agent.sandbox.sdk_context import resolve_sdk_session_fields
from agent.sandbox.session_dir import resolve_agent_session_dir
from agent.sandbox.sbx.policy import mcp_policy_resource_from_url
from agent.utils.mcp_client import load_session_mcp_config

pytestmark = pytest.mark.unit


def test_sandbox_logs_and_commands_redact_secrets() -> None:
    env = format_env_for_log(
        {"OPENAI_API_KEY": "sk-test-secret", "NIKA_AGENT_TYPE": "mock"}
    )
    command = redact_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz")

    assert env == {
        "OPENAI_API_KEY": "***REDACTED***",
        "NIKA_AGENT_TYPE": "mock",
    }
    assert "REDACTED" in command
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in command


def test_sandbox_mcp_servers_use_gateway_and_session_header() -> None:
    servers = build_sandbox_mcp_servers(
        session_id="sess-1",
        scenario_name="simple_bgp",
        backend="kathara",
        gateway_agent_url="http://host.docker.internal:40207",
    )

    assert servers
    assert all(server["transport"] == "http" for server in servers.values())
    assert all(
        server["url"].startswith("http://host.docker.internal:40207/mcp/")
        for server in servers.values()
    )
    assert all(
        server["headers"]["NIKA-Session-Id"] == "sess-1" for server in servers.values()
    )


def test_host_bridge_uses_sandbox_localhost_policy_resource() -> None:
    assert (
        mcp_policy_resource_from_url("http://host.docker.internal:40207/mcp")
        == "localhost:40207"
    )


def test_session_manifest_mcp_config_wins_over_process_environment(tmp_path) -> None:
    baked = {
        "task_mcp_server": {
            "transport": "http",
            "url": "http://host.docker.internal:40207/mcp/task_mcp_server/mcp",
        }
    }
    (tmp_path / "sandbox_manifest.json").write_text(
        json.dumps({"mcp_servers": baked}),
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {
            "NIKA_SANDBOX_EXECUTION": "1",
            "NIKA_MCP_GATEWAY_AGENT_URL": "http://localhost:59999",
        },
        clear=False,
    ):
        actual = load_session_mcp_config(
            "sess-1",
            "simple_bgp",
            session_dir=tmp_path,
        )

    assert actual == baked


def test_sdk_session_fields_are_read_from_sandbox_manifest(tmp_path) -> None:
    (tmp_path / "sandbox_manifest.json").write_text(
        json.dumps({"scenario_name": "simple_bgp"}),
        encoding="utf-8",
    )
    with patch.dict(
        os.environ,
        {"NIKA_SANDBOX_EXECUTION": "1", "NIKA_SESSION_DIR": str(tmp_path)},
        clear=False,
    ):
        session_dir, scenario = resolve_sdk_session_fields("sess-1")

    assert session_dir == str(tmp_path.resolve())
    assert scenario == "simple_bgp"


def test_resolve_agent_session_dir_uses_sandbox_run_for_cli_exec() -> None:
    with patch.dict(
        os.environ,
        {
            "NIKA_SANDBOX_NAME": "nika-test",
            "NIKA_SESSION_DIR": "/tmp/.sandbox_run",
        },
        clear=True,
    ):
        assert resolve_agent_session_dir("/tmp/results/session") == "/tmp/.sandbox_run"


def test_host_reads_refuse_symlinks_planted_in_workspace(tmp_path) -> None:
    """A sandbox cannot point host reads of its workspace at host files."""
    from agent.sandbox.workspace import (
        SandboxWorkspace,
        TraceMirror,
        collect_artifacts,
        read_workspace_text,
    )

    host_secret = tmp_path / "ground_truth.json"
    host_secret.write_text(
        json.dumps({"phase": "diagnosis", "event": "llm_end", "report": "secret"})
        + "\n",
        encoding="utf-8",
    )
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    for name in ("diagnosis_report.json", "messages.jsonl", "sandbox_manifest.json"):
        (workspace_dir / name).symlink_to(host_secret)

    with pytest.raises(OSError):
        read_workspace_text(workspace_dir / "diagnosis_report.json")

    mirror = TraceMirror(
        workspace_dir / "messages.jsonl", session_dir / "messages.jsonl"
    )
    mirror.sync()
    assert not (session_dir / "messages.jsonl").exists()

    collect_artifacts(
        SandboxWorkspace(session_dir=session_dir, workspace_dir=workspace_dir)
    )
    assert not (session_dir / "sandbox_manifest.json").exists()


def test_host_writes_replace_symlinks_planted_in_workspace(tmp_path) -> None:
    """Writing a hand-off file never follows a symlink to a host file."""
    from agent.sandbox.workspace import write_workspace_text

    host_file = tmp_path / "host.txt"
    host_file.write_text("untouched", encoding="utf-8")
    target = tmp_path / "submission_context.json"
    target.symlink_to(host_file)

    write_workspace_text(target, '{"phase": "submission"}')

    assert host_file.read_text(encoding="utf-8") == "untouched"
    assert not target.is_symlink()
    assert json.loads(target.read_text(encoding="utf-8")) == {"phase": "submission"}


def test_e2b_template_name_tracks_pin_and_sizing() -> None:
    """A new image pin, package pin or sandbox size gets its own e2b template."""
    from agent.sandbox.e2b.backend import e2b_template_name, memory_mb

    pinned = "docker/sandbox-templates:shell-docker@sha256:" + "a" * 64
    name = e2b_template_name("shell", pinned, cpus=2, memory=memory_mb("4g"))
    assert name == "nika-shell-aaaaaaaaaaaa-c2-m4096"

    repinned = "docker/sandbox-templates:shell-docker@sha256:" + "b" * 64
    assert e2b_template_name("shell", repinned, cpus=2, memory=4096) != name
    assert e2b_template_name("shell", pinned, cpus=4, memory=4096) != name
    assert e2b_template_name("shell", pinned, cpus=2, memory=memory_mb("512m")) != name

    sdk = e2b_template_name("shell", pinned, cpus=2, memory=4096, packages=["a==1"])
    assert sdk != name
    assert (
        e2b_template_name("shell", pinned, cpus=2, memory=4096, packages=["a==2"])
        != sdk
    )


def test_e2b_connection_uses_url_then_domain_then_cloud(tmp_path, monkeypatch) -> None:
    """A service URL takes every request; otherwise E2B_DOMAIN; else E2B cloud."""
    from dataclasses import replace

    from agent.sandbox.config import SandboxConfig
    from agent.sandbox.e2b.backend import _connection_opts

    for name in ("E2B_API_KEY", "E2B_DOMAIN"):
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("E2B_API_KEY=key\nE2B_DOMAIN=e2b.example.org\n")
    config = SandboxConfig(
        backend="e2b",
        env_file=env_file,
        keep_container=False,
        cpus=2,
        memory="4g",
        offline_sdk_wheels=True,
    )

    assert _connection_opts(config) == {"api_key": "key", "domain": "e2b.example.org"}
    url = "http://localhost:8000"
    assert _connection_opts(replace(config, url=url)) == {
        "api_key": "key",
        "api_url": url,
        "sandbox_url": url,
    }
    env_file.write_text("E2B_API_KEY=key\n")
    assert _connection_opts(config) == {"api_key": "key"}


def test_e2b_async_process_keeps_streams_separate_and_reports_exit() -> None:
    """Host CLI workers read stdout lines and stderr separately, then the code."""
    import asyncio
    from types import SimpleNamespace

    pytest.importorskip("e2b")
    from agent.sandbox.e2b.backend import _AsyncCommandProcess

    class Handle:
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def wait(self, on_stdout, on_stderr):
            on_stdout('{"type": "a"}\n{"ty')
            on_stderr("warning\n")
            on_stdout('pe": "b"}\n')
            if self.fail:
                raise ConnectionError("stream reset")
            return SimpleNamespace(exit_code=0)

        def kill(self) -> None:
            return None

    async def run(fail: bool) -> tuple[list[bytes], bytes, int]:
        proc = _AsyncCommandProcess(Handle(fail))
        lines = [line async for line in proc.stdout]
        return lines, await proc.stderr.read(), await proc.wait()

    lines, stderr, code = asyncio.run(run(fail=False))
    assert lines == [b'{"type": "a"}\n', b'{"type": "b"}\n']
    assert stderr == b"warning\n"
    assert code == 0

    _, stderr, code = asyncio.run(run(fail=True))
    assert code == 1
    assert b"stream reset" in stderr
