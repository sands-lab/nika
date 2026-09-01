"""Contract tests for sandbox SDK wheel staging."""

from __future__ import annotations

from unittest.mock import patch

from agent.sandbox.config import resolve_sandbox_config
from agent.sandbox.sbx.wheels import (
    SDK_WHEEL_DIRNAME,
    install_sdk_wheels_in_sandbox,
    sdk_wheel_dir,
    stage_sdk_wheels,
)


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


def test_resolve_sandbox_config_offline_sdk_wheels_default_off() -> None:
    from nika.run_config.loader import reset_run_config, set_run_config
    from nika.run_config.schema import RunConfig

    reset_run_config()
    set_run_config(RunConfig())
    assert resolve_sandbox_config().offline_sdk_wheels is False

    set_run_config(
        RunConfig.model_validate({"nika": {"sandbox": {"offline_sdk_wheels": True}}})
    )
    assert resolve_sandbox_config().offline_sdk_wheels is True
    assert resolve_sandbox_config(offline_sdk_wheels=False).offline_sdk_wheels is False
    reset_run_config()
