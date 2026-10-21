"""Contract tests for agent wheel staging and requirement sets."""

from __future__ import annotations

import pytest

from unittest.mock import patch

from nika.agents.sandbox.config import resolve_sandbox_config
from nika.agents.registry import BUILTIN_AGENTS
from nika.agents.sandbox.sbx.wheels import install_agent_packages_in_sandbox
from nika.agents.sandbox.wheels import (
    WHEEL_DIRNAME,
    load_requirements,
    needs_agent_venv,
    runner_path_prefix,
    stage_wheels,
    wheel_dir,
)

pytestmark = pytest.mark.unit


def test_agent_wheels_are_staged_and_installed_offline(tmp_path) -> None:
    cache = tmp_path / "cache"
    (cache / "claude_sdk").mkdir(parents=True)
    cache = cache / "claude_sdk"
    (cache / "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with (
        patch("nika.agents.sandbox.wheels._WHEEL_CACHE", cache.parent),
        patch("nika.agents.sandbox.wheels._pip_download"),
    ):
        staged = stage_wheels(workspace, "claude_sdk")
    assert staged == workspace / WHEEL_DIRNAME

    wheels = wheel_dir(workspace)
    with patch("nika.agents.sandbox.sbx.wheels.run_sbx_checked") as run:
        install_agent_packages_in_sandbox(
            sandbox_name="agent-test",
            workspace_dir=workspace,
            requirements="claude_sdk",
            offline=True,
        )

    command = run.call_args.args[0]
    assert command[:2] == ["exec", "agent-test"]
    assert "--no-index" in command[-1]
    assert str(wheels) in command[-1]
    assert "-r '" in command[-1]
    assert (workspace / "requirements.txt").is_file()


def test_agent_packages_install_from_pypi_when_offline_disabled(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("nika.agents.sandbox.sbx.wheels.run_sbx_checked") as run:
        install_agent_packages_in_sandbox(
            sandbox_name="agent-test",
            workspace_dir=workspace,
            requirements="runtime",
            packages=["nooa[mcp]==0.0.10"],
            offline=False,
        )

    base, extra = (call.args[0] for call in run.call_args_list)
    assert base[:2] == ["exec", "agent-test"]
    assert "--no-index" not in base[-1]
    assert "-r '" in base[-1]
    # An added agent's own packages come from PyPI after the frozen set.
    assert "'nooa[mcp]==0.0.10'" in extra[-1]
    assert (workspace / "requirements.txt").is_file()


@pytest.mark.parametrize(
    ("requires_python", "venv"),
    [(None, False), ("", False), (">=3.10", False), (">=3.12,<3.14", True)],
)
def test_agent_venv_only_when_template_python_does_not_fit(
    requires_python: str | None, venv: bool
) -> None:
    assert needs_agent_venv(requires_python) is venv
    assert bool(runner_path_prefix(requires_python)) is venv


def test_agent_packages_install_into_venv_for_other_python(tmp_path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with patch("nika.agents.sandbox.sbx.wheels.run_sbx_checked") as run:
        install_agent_packages_in_sandbox(
            sandbox_name="agent-test",
            workspace_dir=workspace,
            requirements="runtime",
            packages=["nooa[mcp]==0.0.10"],
            requires_python=">=3.12,<3.14",
            offline=True,
        )

    venv, base, extra = (call.args[0][-1] for call in run.call_args_list)
    assert venv == "uv venv --python '>=3.12,<3.14' \"$HOME/.agent-venv\""
    # Offline wheels are built for the template's Python: PyPI instead.
    assert "--no-index" not in base
    assert '--python "$HOME/.agent-venv/bin/python"' in base
    assert f"-r {workspace / 'requirements.txt'}" in base
    assert extra.endswith("'nooa[mcp]==0.0.10'")


@pytest.mark.parametrize(
    "name", sorted({spec.requirements for spec in BUILTIN_AGENTS} | {"langgraph"})
)
def test_agent_requirement_sets_are_exact_pins(name: str) -> None:
    pins = load_requirements(name)
    assert all("==" in req for req in pins)
    # Every agent environment can import the shared runtime (trace logging).
    assert any(req.startswith("langchain-core==") for req in pins)


def test_resolve_sandbox_config_offline_sdk_wheels_default_on() -> None:
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig

    reset_run_config()
    set_run_config(RunConfig())
    assert resolve_sandbox_config().offline_sdk_wheels is True

    set_run_config(
        RunConfig.model_validate({"nika": {"sandbox": {"offline_sdk_wheels": False}}})
    )
    assert resolve_sandbox_config().offline_sdk_wheels is False
    assert resolve_sandbox_config(offline_sdk_wheels=True).offline_sdk_wheels is True
    reset_run_config()
