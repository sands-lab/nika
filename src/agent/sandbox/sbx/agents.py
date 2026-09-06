"""Map NIKA agent types to native Docker Sandboxes agents."""

from __future__ import annotations

NATIVE_SBX_AGENTS: dict[str, str] = {
    "cli.codex": "codex",
    "cli.claude": "claude",
    "sdk.codex_sdk": "shell",
    "sdk.claude_sdk": "shell",
    "community.sade": "shell",
}

# Default images for ``sbx create <agent>`` (Docker Hub ``docker/sandbox-templates``).
NATIVE_SBX_TEMPLATE_IMAGES: dict[str, str] = {
    "codex": "docker/sandbox-templates:codex-docker",
    "claude": "docker/sandbox-templates:claude-code-docker",
    "shell": "docker/sandbox-templates:shell-docker",
}

ENV_SBX_SANDBOX_NAME = "NIKA_SBX_SANDBOX_NAME"


def native_sbx_agent(agent_type: str) -> str:
    try:
        return NATIVE_SBX_AGENTS[agent_type]
    except KeyError as exc:
        raise ValueError(f"No native sbx agent for {agent_type!r}") from exc


def uses_native_sbx_agent(agent_type: str) -> bool:
    return agent_type in NATIVE_SBX_AGENTS


def sbx_template_image(native_agent: str) -> str:
    try:
        return NATIVE_SBX_TEMPLATE_IMAGES[native_agent]
    except KeyError as exc:
        raise ValueError(f"No sbx template image for {native_agent!r}") from exc


def required_sbx_template_images(*agent_types: str) -> list[str]:
    """Return unique template images for sandbox-supported NIKA agent types."""
    images: list[str] = []
    seen: set[str] = set()
    for agent_type in agent_types:
        if not uses_native_sbx_agent(agent_type):
            continue
        image = sbx_template_image(native_sbx_agent(agent_type))
        if image not in seen:
            seen.add(image)
            images.append(image)
    return images
