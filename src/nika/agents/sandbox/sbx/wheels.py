"""Install an agent's Python packages inside an sbx sandbox."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from nika.agents.sandbox.sbx.client import run_sbx_checked
from nika.agents.sandbox.wheels import (
    REQUIREMENTS_FILENAME,
    needs_agent_venv,
    packages_install_command,
    pip_install_command,
    stage_requirements,
    venv_install_commands,
    wheel_dir,
)

logger = logging.getLogger(__name__)


def install_agent_packages_in_sandbox(
    *,
    sandbox_name: str,
    workspace_dir: Path,
    requirements: str,
    packages: Sequence[str] = (),
    requires_python: str | None = None,
    offline: bool,
) -> None:
    """Install requirement set *requirements*, then *packages*, in an sbx sandbox.

    sbx bind-mounts the workspace at its host path, so the host path is also
    the path inside the sandbox. *packages* (an added agent's own pip
    requirements) always come from PyPI. When *requires_python* excludes the
    template's Python, everything goes into a uv virtualenv from PyPI instead.
    """
    stage_requirements(workspace_dir, requirements)
    if needs_agent_venv(requires_python):
        assert requires_python is not None
        logger.info(
            "Installing agent packages into a Python %s venv in %s",
            requires_python,
            sandbox_name,
        )
        for command in venv_install_commands(
            requires_python,
            requirements_file=str(workspace_dir / REQUIREMENTS_FILENAME),
            packages=packages,
        ):
            run_sbx_checked(["exec", sandbox_name, "bash", "-lc", command])
        return
    if offline:
        wheels = wheel_dir(workspace_dir)
        if not any(wheels.glob("*.whl")):
            raise RuntimeError(f"No staged agent wheels found under {wheels}")
        logger.info("Installing agent packages from offline wheels in %s", sandbox_name)
    else:
        logger.info("Installing frozen agent packages from PyPI in %s", sandbox_name)
    inner = pip_install_command(str(workspace_dir), offline=offline)
    run_sbx_checked(["exec", sandbox_name, "bash", "-lc", inner])
    if packages:
        logger.info("Installing the agent's own packages from PyPI in %s", sandbox_name)
        run_sbx_checked(
            ["exec", sandbox_name, "bash", "-lc", packages_install_command(packages)]
        )
