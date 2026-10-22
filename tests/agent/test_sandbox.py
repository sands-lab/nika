from __future__ import annotations

import pytest

import json

from nika.agents.sandbox.env import format_env_for_log
from nika.agents.sandbox.mcp_manifest import build_sandbox_mcp_servers
from nika.agents.sandbox.redact import redact_text
from nika.agents.sandbox.sbx.policy import mcp_policy_resource_from_url
from nika.agents.sandbox.workspace import collect_artifacts, prepare_workspace
from nika.mcp.gateway.session_registry import (
    get_session,
    register_session,
    unregister_session,
)

pytestmark = pytest.mark.unit


def test_sandbox_logs_and_commands_redact_secrets() -> None:
    env = format_env_for_log(
        {"OPENAI_API_KEY": "sk-test-secret", "AGENT_WORKSPACE": "/tmp/ws"}
    )
    command = redact_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz")

    assert env == {
        "OPENAI_API_KEY": "***REDACTED***",
        "AGENT_WORKSPACE": "/tmp/ws",
    }
    assert "REDACTED" in command
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in command


def test_sandbox_mcp_servers_use_gateway_and_session_header(tmp_path) -> None:
    register_session("sess-1", scenario_name="simple_bgp")
    try:
        token = get_session("sess-1").access_token
        servers = build_sandbox_mcp_servers(
            session_id="sess-1",
            scenario_name="simple_bgp",
            backend="kathara",
            gateway_agent_url="http://host.docker.internal:40207",
        )
    finally:
        unregister_session("sess-1")

    assert servers
    assert all(server["transport"] == "http" for server in servers.values())
    assert all(
        server["url"].startswith("http://host.docker.internal:40207/mcp/")
        for server in servers.values()
    )
    assert all(
        server["headers"]["X-Session-Id"] == "sess-1" for server in servers.values()
    )
    assert all(
        server["headers"]["X-Session-Token"] == token for server in servers.values()
    )

    # The task copied into results carries no gateway token.
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    workspace = prepare_workspace(
        session_dir=session_dir,
        task_json=json.dumps({"mcp_servers": servers}),
        runtime_env={},
        agent_session_id="sess-1",
        enable_skills=False,
        workspace_dir=tmp_path / "workspace",
    )
    collect_artifacts(workspace)
    collected = json.loads((session_dir / "sandbox_manifest.json").read_text())
    assert token not in json.dumps(collected)
    assert all(
        server["headers"]["X-Session-Token"] == "***REDACTED***"
        for server in collected["mcp_servers"].values()
    )


def test_host_bridge_uses_sandbox_localhost_policy_resource() -> None:
    assert (
        mcp_policy_resource_from_url("http://host.docker.internal:40207/mcp")
        == "localhost:40207"
    )


def test_host_reads_refuse_symlinks_planted_in_workspace(tmp_path) -> None:
    """A sandbox cannot point host reads of its workspace at host files."""
    from nika.agents.sandbox.workspace import (
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
    for name in ("report.json", "messages.jsonl", "task.json"):
        (workspace_dir / name).symlink_to(host_secret)

    with pytest.raises(OSError):
        read_workspace_text(workspace_dir / "report.json")

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
    from nika.agents.sandbox.workspace import write_workspace_text

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
    from nika.agents.sandbox.e2b.backend import e2b_template_name, memory_mb

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

    # A Python the template lacks builds a venv, so another template; one it
    # has keeps the same name.
    def py(spec: str) -> str:
        return e2b_template_name(
            "shell",
            pinned,
            cpus=2,
            memory=4096,
            packages=["a==1"],
            requires_python=spec,
        )

    assert py(">=3.10") == sdk
    assert py(">=3.12,<3.14") != sdk


def test_e2b_connection_uses_url_then_domain_then_cloud(tmp_path, monkeypatch) -> None:
    """A service URL takes every request; otherwise E2B_DOMAIN; else E2B cloud."""
    from dataclasses import replace

    from nika.agents.sandbox.config import SandboxConfig
    from nika.agents.sandbox.e2b.backend import E2bBackend, _connection_opts

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
    # Sessions record where they ran, so cleanup reaches that service later.
    assert E2bBackend.session_fields(config) == {"sandbox_domain": "e2b.example.org"}
    assert E2bBackend.session_fields(replace(config, url=url)) == {"sandbox_url": url}
    env_file.write_text("E2B_API_KEY=key\n")
    assert _connection_opts(config) == {"api_key": "key"}
    assert E2bBackend.session_fields(config) == {}
    assert _connection_opts(config, domain="e2b.example.org") == {
        "api_key": "key",
        "domain": "e2b.example.org",
    }


def test_e2b_tunnel_reaches_a_local_or_remote_gateway(tmp_path, monkeypatch) -> None:
    """The sandbox dials its loopback; the tunnel connects to where the gateway runs."""
    from nika.agents.sandbox.config import SandboxConfig
    from nika.agents.sandbox.e2b import backend as e2b_backend

    targets: dict[int, tuple[str, int]] = {}
    monkeypatch.setattr(
        e2b_backend,
        "_CommandTunnel",
        lambda _sbx, _relay, mapped: targets.update(mapped),
    )
    backend = e2b_backend.E2bBackend(
        SandboxConfig(
            backend="e2b",
            env_file=tmp_path / ".env",
            keep_container=False,
            cpus=2,
            memory="4g",
            offline_sdk_wheels=True,
        )
    )
    backend._model_proxy_port = 41000
    proxy = {41000: ("127.0.0.1", 41000)}

    def connect(port: int, gateway_url: str) -> str:
        targets.clear()
        backend.allow_gateway("sbx-1", port=port, gateway_url=gateway_url)
        return backend.sandbox_gateway_url(port=port, gateway_url=gateway_url)

    # Local gateway on host loopback.
    assert connect(40000, "http://127.0.0.1:40000") == "http://127.0.0.1:40000"
    assert targets == {40000: ("127.0.0.1", 40000), **proxy}
    # Remote lab host's gateway.
    assert connect(40000, "http://lab.example:40000") == "http://127.0.0.1:40000"
    assert targets == {40000: ("lab.example", 40000), **proxy}
    # A remote gateway on the local model proxy's port gets another sandbox port.
    assert connect(41000, "http://lab.example:41000") == "http://127.0.0.1:40999"
    assert targets == {40999: ("lab.example", 41000), **proxy}
