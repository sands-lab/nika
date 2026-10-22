from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nika.agents.registry import BUILTIN_AGENTS, get_agent_spec
from nika.agents.sandbox.config import resolve_sandbox_config
from nika.agents.sandbox.sbx.agents import (
    native_sbx_agent,
    required_sbx_template_images,
    sbx_template_image,
)
from nika.agents.sandbox.sbx.client import (
    ensure_sbx_daemon,
    require_sbx_authenticated,
    run_sbx,
    run_sbx_checked,
)
from nika.agents.sandbox.model_proxy import require_anthropic_messages_route
from nika.agents.sandbox.manager import SandboxManager
from nika.agents.sandbox.sbx.backend import SbxBackend
from nika.agents.sandbox.sbx.proxy import (
    ensure_sbx_proxy_config,
    resolve_sbx_upstream_proxy,
    sbx_daemon_running,
    sbx_process_env,
)
from nika.agents.sandbox.workspace import collect_artifacts, prepare_workspace
from nika.run_config import default_run_config, reset_run_config, set_run_config

pytestmark = pytest.mark.unit


def _sbx_config(**overrides):
    """Sandbox config for the sbx backend, whatever the host config selects."""
    return replace(resolve_sandbox_config(**overrides), backend="sbx")


@pytest.mark.parametrize(
    ("agent_type", "image"),
    [
        ("cli.codex", "docker/sandbox-templates:codex-docker"),
        ("cli.claude", "docker/sandbox-templates:claude-code-docker"),
        ("cli.opencode", "docker/sandbox-templates:shell-docker"),
        ("cli.dsh", "docker/sandbox-templates:shell-docker"),
        ("sdk.codex_sdk", "docker/sandbox-templates:shell-docker"),
        ("sdk.claude_sdk", "docker/sandbox-templates:shell-docker"),
        ("community.sade", "docker/sandbox-templates:shell-docker"),
        ("byo.langgraph", "docker/sandbox-templates:shell-docker"),
        ("byo.mcp_agent", "docker/sandbox-templates:shell-docker"),
        ("byo.autogen", "docker/sandbox-templates:shell-docker"),
    ],
)
def test_required_sbx_template_images(agent_type: str, image: str) -> None:
    [pinned] = required_sbx_template_images(agent_type)
    assert pinned.startswith(f"{image}@sha256:")
    # The host-run mock agent needs no template.
    assert required_sbx_template_images("mock", agent_type) == [pinned]
    assert sbx_template_image(native_sbx_agent(agent_type)) == pinned


@pytest.mark.parametrize(
    ("agent_type", "sbx_agent"),
    [
        ("cli.codex", "codex"),
        ("cli.claude", "claude"),
        ("cli.opencode", "shell"),
        ("cli.dsh", "shell"),
        ("sdk.codex_sdk", "shell"),
        ("sdk.claude_sdk", "shell"),
        ("community.sade", "shell"),
        ("byo.langgraph", "shell"),
    ],
)
def test_agent_mapping_and_create_command(agent_type: str, sbx_agent: str) -> None:
    set_run_config(default_run_config())
    try:
        backend = SbxBackend(resolve_sandbox_config())
    finally:
        reset_run_config()

    assert native_sbx_agent(agent_type) == sbx_agent
    assert backend.build_create_command(
        sandbox_name="nika-test",
        sbx_agent=sbx_agent,
        workspace_dir=Path("/tmp/ws"),
    ) == [
        "create",
        "--name",
        "nika-test",
        "--template",
        sbx_template_image(sbx_agent),
        sbx_agent,
        "/tmp/ws",
        "--cpus",
        "2",
        "-m",
        "4g",
    ]


@pytest.mark.parametrize(
    "agent_type",
    sorted(spec.id for spec in BUILTIN_AGENTS if spec.template is not None),
)
def test_bundled_agents_construct_without_the_host_package(
    tmp_path, agent_type: str
) -> None:
    """The runner builds every sandboxed agent from the bundled ``agent`` package."""
    import subprocess
    import sys

    spec = get_agent_spec(agent_type)
    SandboxManager._bundle_agent_sources(tmp_path, spec)
    (tmp_path / "task.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "agent_class": spec.class_path,
                "task_description": "diagnose",
                "model": "m1",
                "llm_provider": sorted(spec.providers)[0],
                "mcp_servers": {
                    "task_mcp_server": {"transport": "http", "url": "http://gw/mcp"}
                },
            }
        ),
        encoding="utf-8",
    )
    # As in the sandbox: only the workspace on the path, and no host package.
    script = (
        "import sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        "sys.modules['nika'] = None\n"
        "from agent.runtime.__main__ import create_agent\n"
        "from agent.runtime.task import current_task\n"
        "create_agent(current_task())\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"AGENT_WORKSPACE": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_workspace_roundtrip_keeps_only_standard_artifacts(tmp_path) -> None:
    from nika.agents.sandbox.workspace import cleanup_workspace

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "ground_truth.json").write_text("secret", encoding="utf-8")
    (session_dir / "run.json").write_text(
        json.dumps(
            {
                "session_id": "sess-1",
                "scenario_name": "simple_bgp",
                "backend": "kathara",
                "status": "running",
                "problem_names": ["link_down"],
                "failure_domain": "link_interface",
                "task_description": "should not leak",
            }
        ),
        encoding="utf-8",
    )
    workspace = prepare_workspace(
        session_dir=session_dir,
        task_json=json.dumps({"session_id": "sess-1", "task_description": "diagnose"}),
        runtime_env={},
        enable_skills=False,
        agent_session_id="sess-1",
        workspace_dir=tmp_path / "agent_workspace",
    )

    # Session metadata (injected failure labels included) stays on the host.
    assert not (workspace.workspace_dir / "ground_truth.json").exists()
    assert not (workspace.workspace_dir / "run.json").exists()
    assert json.loads(workspace.task_path.read_text())["session_id"] == "sess-1"

    (workspace.workspace_dir / "messages.jsonl").write_text("line\n", encoding="utf-8")
    (workspace.workspace_dir / "submission.json").write_text("{}", encoding="utf-8")
    sdk_workspace = workspace.workspace_dir / "codex_sdk_workspace"
    sdk_workspace.mkdir()
    (sdk_workspace / "keep.txt").write_text("ok", encoding="utf-8")
    collect_artifacts(workspace)
    cleanup_workspace(workspace)

    # The host trace is host-written; the workspace copy is not collected.
    assert not (session_dir / "messages.jsonl").exists()
    # Agent-writable workspace submissions must not reach scoring.
    assert not (session_dir / "submission.json").exists()
    assert (session_dir / "sandbox_manifest.json").is_file()
    assert not workspace.workspace_dir.exists()
    assert not (session_dir / "codex_sdk_workspace").exists()


def test_trace_mirror_keeps_host_freeze_authoritative(tmp_path) -> None:
    """In-VM SDK traces reach the host live, minus planted freezes and other phases."""
    from nika.agents.sandbox.workspace import TraceMirror

    source = tmp_path / "workspace" / "messages.jsonl"
    source.parent.mkdir()
    dest = tmp_path / "session" / "messages.jsonl"
    dest.parent.mkdir()
    mirror = TraceMirror(source, dest)

    def event(phase: str, name: str, **fields) -> str:
        return json.dumps({"phase": phase, "event": name, **fields}) + "\n"

    source.write_text(
        event("diagnosis", "llm_end", text="bgp down")
        + event("diagnosis", "diagnosis_frozen", report="planted")
        + '{"phase": "diagnosis", "event": "tool_start"',  # incomplete line
        encoding="utf-8",
    )
    mirror.sync()
    rows = [json.loads(line) for line in dest.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["llm_end"]

    with source.open("a", encoding="utf-8") as handle:
        handle.write("}\n")
    mirror.set_phase("submission")
    # Diagnosis events written after the host froze the report are dropped.
    with source.open("a", encoding="utf-8") as handle:
        handle.write(event("diagnosis", "llm_end", text="late"))
        handle.write(event("submission", "tool_start", tool={"name": "submit"}))
    mirror.stop()
    rows = [json.loads(line) for line in dest.read_text().splitlines()]
    assert [(r["phase"], r["event"]) for r in rows] == [
        ("diagnosis", "llm_end"),
        ("diagnosis", "tool_start"),
        ("submission", "tool_start"),
    ]


def test_collect_artifacts_preserves_host_mcp_submission(tmp_path) -> None:
    """Host MCP submit() wins over a sandbox workspace submission.json on collect."""
    from nika.agents.sandbox.workspace import cleanup_workspace

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "run.json").write_text(
        json.dumps(
            {
                "session_id": "sess-submit",
                "scenario_name": "simple_bgp",
                "backend": "kathara",
                "status": "running",
            }
        ),
        encoding="utf-8",
    )
    host_submission = {
        "diagnosis_report": "frozen report",
        "is_anomaly": True,
        "root_causes": [
            {
                "resource_id": "link/a:eth0--b:eth0",
                "fault_type": "link_down",
            }
        ],
    }
    (session_dir / "submission.json").write_text(
        json.dumps(host_submission),
        encoding="utf-8",
    )
    workspace = prepare_workspace(
        session_dir=session_dir,
        task_json=json.dumps(
            {"session_id": "sess-submit", "task_description": "diagnose"}
        ),
        runtime_env={},
        enable_skills=False,
        agent_session_id="sess-submit",
        workspace_dir=tmp_path / "agent_workspace",
    )
    (workspace.workspace_dir / "messages.jsonl").write_text(
        "agent-log\n", encoding="utf-8"
    )
    (workspace.workspace_dir / "submission.json").write_text(
        json.dumps({"is_anomaly": False, "root_causes": []}),
        encoding="utf-8",
    )

    collect_artifacts(workspace)
    cleanup_workspace(workspace)

    assert not (session_dir / "messages.jsonl").exists()
    kept = json.loads((session_dir / "submission.json").read_text(encoding="utf-8"))
    assert kept == host_submission


def test_opaque_workspace_hides_case_key_from_agent_surfaces(
    tmp_path, monkeypatch
) -> None:
    """Readable trial ids must not appear in the agent's cwd or hostname."""
    from nika.agents.sandbox.sbx.policy import sanitize_sandbox_name
    from nika.agents.sandbox.workspace import (
        cleanup_workspace,
        opaque_agent_workspace_dir,
    )

    from nika.agents.sandbox.workspace import agent_workspaces_root

    # Sandboxes see the workspace at its host path: it names no project.
    assert "nika" not in str(agent_workspaces_root()).lower()
    monkeypatch.setattr(
        "nika.agents.sandbox.workspace.agent_workspaces_root",
        lambda: tmp_path / "workspaces",
    )
    case_key = (
        "campus_lan__dhcp_missing_subnet__m__host_name-dhcp_server__"
        "host_name_2-pc_1_1_1_1__subnet-10.1.1.0__t01"
    )
    agent_sid = "20260101-120000-a-aabbcc"
    session_dir = tmp_path / "trials" / case_key
    session_dir.mkdir(parents=True)
    (session_dir / "run.json").write_text(
        json.dumps(
            {
                "session_id": case_key,
                "agent_session_id": agent_sid,
                "scenario_name": "campus_lan",
                "backend": "kathara",
                "status": "running",
                "problem_names": ["dhcp_missing_subnet"],
            }
        ),
        encoding="utf-8",
    )
    workspace = prepare_workspace(
        session_dir=session_dir,
        task_json=json.dumps({"session_id": agent_sid, "scenario_name": "campus_lan"}),
        runtime_env={},
        enable_skills=False,
        agent_session_id=agent_sid,
    )
    ws = str(workspace.workspace_dir)
    assert "dhcp_missing_subnet" not in ws
    assert "host_name-dhcp_server" not in ws
    assert case_key not in ws
    assert opaque_agent_workspace_dir(agent_sid) == workspace.workspace_dir
    assert not (workspace.workspace_dir / "run.json").exists()
    sbx_name = sanitize_sandbox_name(agent_sid)
    assert "dhcp_missing" not in sbx_name
    assert "campus_lan" not in sbx_name
    cleanup_workspace(workspace)
    # Host trial dir layout unchanged.
    assert session_dir.is_dir()
    assert (session_dir / "run.json").is_file()


def test_task_uses_opaque_session_id(tmp_path) -> None:
    session = SimpleNamespace(
        session_id=("campus_lan__dhcp_missing_subnet__m__host_name-dhcp_server__t01"),
        agent_session_id="20260101-120000-a-ccddee",
        session_dir=str(tmp_path),
        task_description="diagnose the network",
        scenario_name="campus_lan",
        backend="kathara",
    )
    manager = SandboxManager(_sbx_config())
    task = manager.build_task(
        session=session,
        spec=get_agent_spec("sdk.codex_sdk"),
        model="gpt-test",
        max_steps=5,
        reasoning_effort=None,
        max_tokens=8192,
        llm_provider="openai",
        mcp_gateway_agent_url="http://host.docker.internal:9999",
        stream_output=False,
    )
    assert task.session_id == "20260101-120000-a-ccddee"
    assert task.agent_class == "agent.sdk.codex_sdk.agent:CodexSdkAgent"
    assert task.max_tokens == 8192
    headers = next(iter(task.mcp_servers.values()))["headers"]
    assert headers["X-Session-Id"] == "20260101-120000-a-ccddee"
    assert "nika" not in task.to_json().lower()


def test_open_session_collects_artifacts_when_policy_cleanup_fails(
    tmp_path, monkeypatch
) -> None:
    # Stands in for the gateway token that the manifest's MCP headers carry.
    monkeypatch.setenv("NIKA_MCP_GATEWAY_SESSION_TOKEN", "gateway-token-secret")
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session = SimpleNamespace(
        session_id="sess-cleanup-failure",
        session_dir=str(session_dir),
        task_description="diagnose",
        scenario_name="simple_bgp",
        backend="kathara",
    )
    manager = SandboxManager(_sbx_config(keep_container=False))

    with (
        patch("nika.agents.sandbox.sbx.backend.sbx_available", return_value=True),
        patch("nika.agents.sandbox.sbx.backend.ensure_sbx_proxy_config"),
        patch("nika.agents.sandbox.sbx.backend.ensure_sbx_ready"),
        patch("nika.agents.sandbox.sbx.backend.require_sbx_authenticated"),
        patch(
            "nika.agents.sandbox.sbx.backend.start_model_proxy",
            return_value=(41000, {"OPENAI_API_KEY": "proxy-token-test"}),
        ),
        patch("nika.agents.sandbox.sbx.backend.run_sbx_checked"),
        patch("nika.agents.sandbox.sbx.backend.run_sbx_optional"),
        patch(
            "nika.agents.sandbox.sbx.policy.run_sbx_checked",
            return_value=SimpleNamespace(stdout='{"rules": []}'),
        ),
        patch("nika.agents.sandbox.sbx.policy.run_sbx_optional"),
        patch("nika.agents.sandbox.sbx.backend.stage_wheels"),
        patch("nika.agents.sandbox.sbx.backend.install_agent_packages_in_sandbox"),
        patch("nika.agents.sandbox.sbx.backend.allow_mcp_gateway"),
        patch(
            "nika.agents.sandbox.sbx.backend.deny_mcp_gateway",
            side_effect=OSError("policy cleanup failed"),
        ),
        patch("nika.agents.sandbox.manager.log_event"),
    ):
        with pytest.raises(OSError, match="policy cleanup failed"):
            with manager.open_session(
                session=session,
                agent_type="cli.codex",
                model="gpt-5-mini",
                max_steps=10,
                reasoning_effort=None,
                max_tokens=8192,
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
    # The trace reaches the host only through the mirror, as valid phase events.
    assert not (session_dir / "messages.jsonl").exists()
    # Only host MCP submit() writes submission.json; workspace copies are ignored.
    assert not (session_dir / "submission.json").exists()
    assert not (session_dir / ".sandbox_run").exists()
    collected = json.loads(
        (session_dir / "sandbox_manifest.json").read_text(encoding="utf-8")
    )
    assert "session_dir" not in collected
    assert str(session_dir) not in json.dumps(collected)
    assert "gateway-token-secret" not in json.dumps(collected)


def test_open_session_preserves_host_submission_on_collect(tmp_path) -> None:
    """MCP submit on the host must survive sandbox workspace collect."""
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    host_submission = {
        "diagnosis_report": "report",
        "is_anomaly": True,
        "root_causes": [
            {"resource_id": "link/x:eth0--y:eth0", "fault_type": "link_down"}
        ],
    }
    (session_dir / "submission.json").write_text(
        json.dumps(host_submission),
        encoding="utf-8",
    )
    session = SimpleNamespace(
        session_id="sess-preserve-submit",
        session_dir=str(session_dir),
        task_description="diagnose",
        scenario_name="simple_bgp",
        backend="kathara",
    )
    manager = SandboxManager(_sbx_config(keep_container=False))

    with (
        patch("nika.agents.sandbox.sbx.backend.sbx_available", return_value=True),
        patch("nika.agents.sandbox.sbx.backend.ensure_sbx_proxy_config"),
        patch("nika.agents.sandbox.sbx.backend.ensure_sbx_ready"),
        patch("nika.agents.sandbox.sbx.backend.require_sbx_authenticated"),
        patch(
            "nika.agents.sandbox.sbx.backend.start_model_proxy",
            return_value=(41000, {"OPENAI_API_KEY": "proxy-token-test"}),
        ),
        patch("nika.agents.sandbox.sbx.backend.run_sbx_checked"),
        patch("nika.agents.sandbox.sbx.backend.run_sbx_optional"),
        patch(
            "nika.agents.sandbox.sbx.policy.run_sbx_checked",
            return_value=SimpleNamespace(stdout='{"rules": []}'),
        ),
        patch("nika.agents.sandbox.sbx.policy.run_sbx_optional"),
        patch("nika.agents.sandbox.sbx.backend.stage_wheels"),
        patch("nika.agents.sandbox.sbx.backend.install_agent_packages_in_sandbox"),
        patch("nika.agents.sandbox.sbx.backend.allow_mcp_gateway"),
        patch("nika.agents.sandbox.sbx.backend.deny_mcp_gateway"),
        patch("nika.agents.sandbox.manager.log_event"),
        manager.open_session(
            session=session,
            agent_type="cli.codex",
            model="gpt-5-mini",
            max_steps=10,
            reasoning_effort=None,
            max_tokens=8192,
            llm_provider="openai",
            mcp_gateway_agent_url="http://host.docker.internal:12345",
            gateway_port=12345,
            stream_output=False,
        ) as sbx_session,
    ):
        (sbx_session.workspace_dir / "messages.jsonl").write_text(
            "message\n", encoding="utf-8"
        )
        (sbx_session.workspace_dir / "submission.json").write_text(
            json.dumps({"is_anomaly": False, "root_causes": []}),
            encoding="utf-8",
        )

    kept = json.loads((session_dir / "submission.json").read_text(encoding="utf-8"))
    assert kept == host_submission
    assert not (session_dir / "messages.jsonl").exists()


def test_e2b_refuses_a_sandbox_with_internet_access(tmp_path) -> None:
    from nika.agents.sandbox.e2b.backend import E2bBackend

    backend = E2bBackend(_sbx_config())
    with patch.object(
        backend, "_run_checked", side_effect=RuntimeError("reached example.com")
    ):
        with pytest.raises(RuntimeError, match="does not enforce egress"):
            backend._check_no_egress()
    with patch.object(backend, "_run_checked", return_value=""):
        backend._check_no_egress()


def test_sandbox_manifest_omits_host_session_dir(tmp_path) -> None:
    """Host session_dir is a shortcut to ground_truth; never bake it into sandbox."""
    from nika.utils.session_store import SessionStore

    session_dir = tmp_path / "session"
    session_dir.mkdir()
    session_id = "sess-no-shortcut"
    store = SessionStore()
    store.create_session(
        {
            "session_id": session_id,
            "lab_name": "lab",
            "scenario_name": "simple_bgp",
            "scenario_topo_size": None,
            "scenario_params": {"backend": "kathara"},
            "session_dir": str(session_dir),
            "status": "running",
            "backend": "kathara",
        }
    )
    session = SimpleNamespace(
        session_id=session_id,
        session_dir=str(session_dir),
        task_description="diagnose",
        scenario_name="simple_bgp",
        backend="kathara",
    )
    manager = SandboxManager(_sbx_config(keep_container=False))
    try:
        with (
            patch(
                "nika.agents.sandbox.manager.build_sandbox_mcp_servers",
                return_value={"task_mcp_server": {"transport": "http", "url": "u"}},
            ),
        ):
            manifest = manager.build_task(
                session=session,
                spec=get_agent_spec("sdk.claude_sdk"),
                model="deepseek-v4-flash",
                max_steps=10,
                reasoning_effort=None,
                max_tokens=8192,
                llm_provider="deepseek",
                mcp_gateway_agent_url="http://host.docker.internal:12345",
                stream_output=False,
            )
        text = manifest.to_json()
        assert "session_dir" not in text
        assert str(session_dir) not in text
        assert "ground_truth" not in text
        assert "submission_context" not in text
        workspace = prepare_workspace(
            session_dir=session_dir,
            task_json=text,
            runtime_env={},
            enable_skills=False,
            agent_session_id=session_id,
            workspace_dir=session_dir.parent / "agent_workspace",
        )
        try:
            content = workspace.task_path.read_text(encoding="utf-8")
            assert "submission_context" not in content
            assert "fault_ontology" not in content
            assert "resources" not in json.loads(content)
        finally:
            from nika.agents.sandbox.workspace import cleanup_workspace

            cleanup_workspace(workspace)
    finally:
        store.delete_session(session_id)


@pytest.mark.parametrize("status", [400, 401, 503])
def test_messages_route_probe_accepts_non_404(status: int) -> None:
    response = SimpleNamespace(status_code=status)
    with patch("nika.agents.sandbox.model_proxy.httpx.post", return_value=response):
        require_anthropic_messages_route("http://ollama:11434")


def test_messages_route_probe_rejects_missing_route() -> None:
    response = SimpleNamespace(status_code=404)
    with (
        patch(
            "nika.agents.sandbox.model_proxy.httpx.post", return_value=response
        ) as post,
        pytest.raises(RuntimeError, match="no Anthropic Messages route"),
    ):
        require_anthropic_messages_route("http://ollama:11434/")
    assert post.call_args.args[0] == "http://ollama:11434/v1/messages"


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
        patch("nika.agents.sandbox.sbx.proxy.sbx_available", return_value=True),
        patch("nika.agents.sandbox.sbx.proxy.subprocess.run") as run,
        patch("nika.agents.sandbox.sbx.proxy.subprocess.Popen") as popen,
    ):
        ensure_sbx_proxy_config(None)
    run.assert_not_called()
    popen.assert_not_called()


def test_ensure_sbx_proxy_config_warns_when_daemon_stays_down() -> None:
    with (
        patch("nika.agents.sandbox.sbx.proxy.sbx_available", return_value=True),
        patch("nika.agents.sandbox.sbx.proxy.subprocess.run") as run,
        patch("nika.agents.sandbox.sbx.proxy.subprocess.Popen") as popen,
        patch("nika.agents.sandbox.sbx.proxy.time.sleep"),
        patch("nika.agents.sandbox.sbx.proxy.sbx_daemon_running", return_value=False),
        patch("nika.agents.sandbox.sbx.proxy.logger.warning") as warning,
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
    import nika.agents.sandbox.sbx.proxy as proxy_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(proxy_mod, "_applied_proxy", None)
    with (
        patch("nika.agents.sandbox.sbx.proxy.sbx_available", return_value=True),
        patch("nika.agents.sandbox.sbx.proxy.sbx_daemon_running", return_value=True),
        patch(
            "nika.agents.sandbox.sbx.proxy._daemon_proxy_matches", return_value=False
        ),
        patch("nika.agents.sandbox.sbx.proxy.subprocess.run") as run,
        patch("nika.agents.sandbox.sbx.proxy.subprocess.Popen") as popen,
        patch("nika.agents.sandbox.sbx.proxy.logger.warning") as warning,
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
            patch("nika.agents.sandbox.sbx.client.sbx_available", return_value=True),
            patch("nika.agents.sandbox.sbx.client.subprocess.run") as run,
        ):
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            run_sbx(["ls"], check=False)
        env = run.call_args.kwargs["env"]
        assert env["HTTPS_PROXY"] == "http://proxy.test:8080"
        assert env["DOCKER_SANDBOXES_PROXY"] == "http://proxy.test:8080"
    finally:
        reset_run_config()


def test_ensure_sbx_daemon_uses_status_not_ls() -> None:
    with (
        patch(
            "nika.agents.sandbox.sbx.proxy.sbx_daemon_running", return_value=True
        ) as status,
        patch("nika.agents.sandbox.sbx.client.subprocess.run") as run,
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
        patch("nika.agents.sandbox.sbx.proxy.socket.socket", return_value=probe),
        patch("nika.agents.sandbox.sbx.proxy.subprocess.run") as run,
    ):
        assert sbx_daemon_running() is True
    run.assert_not_called()


def test_require_sbx_authenticated_fails_with_actionable_message() -> None:
    with (
        patch("nika.agents.sandbox.sbx.client.sbx_authenticated", return_value=False),
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
        patch("nika.agents.sandbox.sbx.client.sbx_available", return_value=True),
        patch(
            "nika.agents.sandbox.sbx.client.subprocess.run", side_effect=[fail, ok]
        ) as run,
        patch("nika.agents.sandbox.sbx.client.time.sleep") as sleep,
        patch.dict(os.environ, {}, clear=True),
    ):
        result = run_sbx_checked(["create", "--name", "nika-test", "claude", "/tmp"])
    assert result.returncode == 0
    assert run.call_count == 2
    sleep.assert_called_once_with(2.0)
