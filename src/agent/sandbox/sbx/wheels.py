"""Install SDK Python packages inside an sbx shell sandbox."""

from __future__ import annotations

import logging
from pathlib import Path

from agent.sandbox.sbx.client import run_sbx_checked
from agent.sandbox.wheels import (
    sdk_pip_install_command,
    sdk_wheel_dir,
    stage_sdk_requirements,
)

logger = logging.getLogger(__name__)


def install_sdk_packages_in_sandbox(
    *,
    sandbox_name: str,
    workspace_dir: Path,
    offline: bool,
) -> None:
    """Install SDK Python deps inside an existing sbx shell sandbox.

    sbx bind-mounts the workspace at its host path, so the host path is also
    the path inside the sandbox.
    """
    stage_sdk_requirements(workspace_dir)
    if offline:
        wheels = sdk_wheel_dir(workspace_dir)
        if not any(wheels.glob("*.whl")):
            raise RuntimeError(f"No staged SDK wheels found under {wheels}")
        logger.info("Installing SDK packages from offline wheels in %s", sandbox_name)
    else:
        logger.info("Installing frozen SDK packages from PyPI in %s", sandbox_name)
    inner = sdk_pip_install_command(str(workspace_dir), offline=offline)
    run_sbx_checked(["exec", sandbox_name, "bash", "-lc", inner])
