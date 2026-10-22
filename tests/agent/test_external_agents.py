"""Agents developed outside NIKA: manifest contract, registration, and resolution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nika.agents.plugins import ENV_AGENTS_FILE, AgentManifest, read_registered_agents
from nika.agents.provider_env import map_provider_credentials
from nika.agents.registry import get_agent_spec
from nika.cli.main import app

pytestmark = pytest.mark.unit

_AGENT_SOURCE = """
from agent.base import DiagnosisAgent
from some_framework import Investigator


class ExternalAgent(DiagnosisAgent):
    async def diagnose(self, task_description):
        return await Investigator().run(task_description)
"""

_MANIFEST = """
id: ext-agent
class: ext_agent_pkg.agent:ExternalAgent
providers: [anthropic, custom]
credential_family: anthropic
sandbox:
  install: [some-framework==1.2]
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

    # Its framework is not installed for NIKA: the agent runs in its sandbox.
    added = runner.invoke(app, ["agent", "add", str(agent_project)])
    assert added.exit_code == 0, added.output
    assert "some-framework==1.2" in added.output
    assert read_registered_agents() == {"ext-agent": {"path": str(agent_project)}}

    listed = runner.invoke(app, ["agent", "list"])
    assert "ext-agent" in listed.output
    assert "sandbox: shell" in listed.output

    spec = get_agent_spec("ext-agent")
    assert spec.source == "user"
    assert spec.template == "shell"
    assert spec.packages == ("some-framework==1.2",)
    assert spec.egress == ("api.example.com",)
    assert spec.import_root == str(agent_project / "src")
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
    (agent_project / "pyproject.toml").write_text(
        '[project]\nname = "ext-agent"\nversion = "0"\n'
        'dependencies = ["some-framework==1.2"]\n',
        encoding="utf-8",
    )
    sys.path.insert(0, str(agent_project / "src"))
    spec = get_agent_spec("ext_agent_pkg.agent:ExternalAgent")
    assert spec.source == "import path"
    assert spec.template == "shell"
    assert spec.import_root == str(agent_project / "src")
    # The project's own dependencies are installed in its sandbox.
    assert spec.packages == ("some-framework==1.2",)


def test_add_rejects_unknown_class(agent_project: Path) -> None:
    manifest = agent_project / "nika-agent.yaml"
    manifest.write_text(
        _MANIFEST.replace("ExternalAgent", "MissingAgent"), encoding="utf-8"
    )
    result = CliRunner().invoke(app, ["agent", "add", str(agent_project)])
    assert result.exit_code != 0
    assert read_registered_agents() == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", "byo.langgraph"), ("id", "Bad Id"), ("class", "no_colon")],
)
def test_manifest_rejects_invalid_fields(field: str, value: str) -> None:
    data = {"id": "ok", "class": "pkg.mod:Agent", field: value}
    with pytest.raises(ValueError):
        AgentManifest.model_validate(data)
