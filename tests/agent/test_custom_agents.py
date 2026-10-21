"""One-file agents under ``src/agent/custom`` are agent types ``custom.<name>``."""

from __future__ import annotations

import pytest

from nika.agents import custom
from nika.agents.registry import ALL_PROVIDERS, get_agent_spec

pytestmark = pytest.mark.unit

_AGENT = """
from agent.base import DiagnosisAgent


class EchoAgent(DiagnosisAgent):
    dependencies = ["requests==2.33.0"]
    requires_python = ">=3.12,<3.14"
    providers = ["openai", "custom"]

    async def diagnose(self, task_description: str) -> str:
        return task_description
"""


@pytest.fixture
def custom_root(tmp_path, monkeypatch):
    monkeypatch.setattr(custom, "custom_dir", lambda: tmp_path)
    return tmp_path


def test_a_file_is_an_agent_type_with_its_declared_needs(custom_root) -> None:
    (custom_root / "echo.py").write_text(_AGENT)
    spec = get_agent_spec("custom.echo")
    assert spec.class_path == "agent.custom.echo:EchoAgent"
    assert spec.packages == ("requests==2.33.0",)
    assert spec.requires_python == ">=3.12,<3.14"
    assert spec.providers == {"openai", "custom"}
    # Same execution path as built-in agents: a sandbox, with an API key.
    assert spec.template == "shell" and not spec.subscription


def test_declarations_are_optional(custom_root) -> None:
    (custom_root / "plain.py").write_text(
        "from agent.base import DiagnosisAgent\n\n"
        "class Plain(DiagnosisAgent):\n"
        "    async def diagnose(self, task_description):\n"
        "        return 'ok'\n"
    )
    spec = get_agent_spec("custom.plain")
    assert (spec.packages, spec.requires_python) == ((), None)
    assert spec.providers == ALL_PROVIDERS


@pytest.mark.parametrize(
    ("source", "error"),
    [
        ("class A:\n    pass\n", "exactly one DiagnosisAgent subclass"),
        (
            "from agent.base import DiagnosisAgent\n"
            "class A(DiagnosisAgent):\n    providers = ['gemini']\n",
            "unknown providers",
        ),
        (
            "from agent.base import DiagnosisAgent\n"
            "DEPS = ['x']\n"
            "class A(DiagnosisAgent):\n    dependencies = DEPS\n",
            "must be a literal",
        ),
    ],
)
def test_invalid_files_are_reported(custom_root, source: str, error: str) -> None:
    (custom_root / "bad.py").write_text(source)
    with pytest.raises(ValueError, match=error):
        get_agent_spec("custom.bad")
    [(agent_id, spec, problem)] = custom.list_custom_agents()
    assert (agent_id, spec) == ("custom.bad", None) and error in problem


def test_unknown_and_private_files_are_not_agents(custom_root) -> None:
    (custom_root / "_helpers.py").write_text(_AGENT)
    for agent_type in ("custom.missing", "custom._helpers"):
        with pytest.raises(ValueError, match="Unsupported agent type"):
            get_agent_spec(agent_type)
    assert custom.list_custom_agents() == []
