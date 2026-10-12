"""Agents developed outside NIKA: manifest contract, registration, and resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent.plugins import ENV_AGENTS_FILE, AgentManifest, read_registered_agents
from agent.registry import create_agent, get_agent_spec
from agent.utils.provider_env import map_provider_credentials
from nika.cli.main import app

pytestmark = pytest.mark.unit

_AGENT_SOURCE = """
class ExternalAgent:
    def __init__(self, session_id, model, llm_provider):
        self.session_id = session_id
        self.model = model
        self.llm_provider = llm_provider

    async def run(self, task_description):
        return {"diagnosis_report": task_description}
"""

_MANIFEST = """
id: ext-agent
class: ext_agent_pkg.agent:ExternalAgent
providers: [anthropic, custom]
credential_family: anthropic
sandbox:
  base: python:3.12-slim
  install: [pip install /nika/agent-src]
  egress: [api.example.com]
"""


@pytest.fixture
def agent_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # src layout; `nika agent add` must find the package without installing it.
    project = tmp_path / "ext-agent"
    package = project / "src" / "ext_agent_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "agent.py").write_text(_AGENT_SOURCE, encoding="utf-8")
    (project / "nika-agent.yaml").write_text(_MANIFEST, encoding="utf-8")
    monkeypatch.setenv(ENV_AGENTS_FILE, str(tmp_path / "agents.yaml"))
    monkeypatch.setattr(sys, "path", list(sys.path))
    yield project
    sys.modules.pop("ext_agent_pkg.agent", None)
    sys.modules.pop("ext_agent_pkg", None)


def test_add_list_run_remove_external_agent(agent_project: Path) -> None:
    runner = CliRunner()

    added = runner.invoke(app, ["agent", "add", str(agent_project)])
    assert added.exit_code == 0, added.output
    assert read_registered_agents() == {"ext-agent": {"path": str(agent_project)}}

    listed = runner.invoke(app, ["agent", "list"])
    assert "ext-agent" in listed.output
    assert "sandbox declared" in listed.output

    spec = get_agent_spec("ext-agent")
    assert spec.source == "user"
    assert spec.sandbox_definition["egress"] == ["api.example.com"]
    # Only the settings the constructor declares are passed.
    agent = create_agent(
        "ext-agent",
        session_id="s1",
        model="m1",
        llm_provider="custom",
        max_tokens=128,
    )
    assert (agent.session_id, agent.model, agent.llm_provider) == ("s1", "m1", "custom")
    # credential_family routes custom endpoints to Anthropic-style env vars.
    env = map_provider_credentials(
        agent_type="ext-agent",
        provider="custom",
        sources={
            "NIKA_CUSTOM_BASE_URL": "http://gw:8000/v1",
            "NIKA_CUSTOM_API_KEY": "k",
        },
    )
    assert env["ANTHROPIC_BASE_URL"] == "http://gw:8000"
    with pytest.raises(ValueError, match="not supported for agent"):
        map_provider_credentials(agent_type="ext-agent", provider="openai", sources={})

    removed = runner.invoke(app, ["agent", "remove", "ext-agent"])
    assert removed.exit_code == 0, removed.output
    assert read_registered_agents() == {}


def test_import_path_runs_without_registration(agent_project: Path) -> None:
    sys.path.insert(0, str(agent_project / "src"))
    agent = create_agent(
        "ext_agent_pkg.agent:ExternalAgent",
        session_id="s1",
        model="m1",
        llm_provider="openai",
    )
    assert agent.model == "m1"
    assert get_agent_spec("ext_agent_pkg.agent:ExternalAgent").source == "import path"


def test_add_rejects_unloadable_class(agent_project: Path) -> None:
    manifest = agent_project / "nika-agent.yaml"
    manifest.write_text(
        _MANIFEST.replace("ExternalAgent", "MissingAgent"), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["agent", "add", str(agent_project)])
    assert result.exit_code != 0
    assert read_registered_agents() == {}


def test_add_reports_missing_dependencies(agent_project: Path) -> None:
    (agent_project / "pyproject.toml").write_text(
        '[project]\nname = "ext-agent"\nversion = "0"\n'
        'dependencies = ["pyyaml", "nika-missing-dep>=1"]\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["agent", "add", str(agent_project)])
    assert result.exit_code != 0
    assert "nika-missing-dep>=1" in result.output
    assert "pyyaml" not in result.output
    assert read_registered_agents() == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "byo.langgraph"), ("id", "Bad Id"), ("class", "no_colon")],
)
def test_manifest_rejects_invalid_fields(field: str, value: str) -> None:
    data = {"id": "ok", "class": "pkg.mod:Agent", field: value}
    with pytest.raises(ValueError):
        AgentManifest.model_validate(data)
