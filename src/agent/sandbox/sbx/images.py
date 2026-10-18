"""Preload Docker Sandboxes template images used by NIKA agents."""

from __future__ import annotations

from collections.abc import Iterable

from agent.sandbox.sbx.agents import required_sbx_template_images, uses_native_sbx_agent


def ensure_sbx_template_images(agent_types: Iterable[str]) -> None:
    """Pull missing native sbx template images for the given NIKA agent types.

    No-op when none of the types use a native sbx agent. Uses the same Docker
    ensure/pull path as lab images so pulls are non-interactive.
    """
    images = required_sbx_template_images(*agent_types)
    if not images:
        return
    from nika.net_env.utils.kathara.docker_files.docker_images import (
        ensure_nika_docker_images,
    )

    ensure_nika_docker_images(images)


def ensure_configured_sbx_template_images() -> None:
    """If run config selects a sandbox-supported agent, ensure its template image."""
    from nika.run_config.loader import get_run_config

    agent_type = get_run_config().agent.type
    if not uses_native_sbx_agent(agent_type):
        return
    ensure_sbx_template_images([agent_type])
