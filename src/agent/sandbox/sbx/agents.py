"""Map NIKA agent types to native Docker Sandboxes agents."""

from __future__ import annotations

NATIVE_SBX_AGENTS: dict[str, str] = {
    "cli.codex": "codex",
    "cli.claude": "claude",
    "sdk.codex_sdk": "shell",
    "sdk.claude_sdk": "shell",
    "community.sade": "shell",
}

# Template images for ``sbx create <agent>`` (Docker Hub ``docker/sandbox-templates``),
# pinned to the last verified digest and passed explicitly with ``--template``.
NATIVE_SBX_TEMPLATE_IMAGES: dict[str, str] = {
    "codex": "docker/sandbox-templates:codex-docker@sha256:8b4cd0a46c8b600bc6b6a64af23c03d4c2807fbfc61f47568092a93fb9dc88b0",
    "claude": "docker/sandbox-templates:claude-code-docker@sha256:549730947ed8a43182547bb5628245fe6d6c089564dd73d2194e899f791d970f",
    "shell": "docker/sandbox-templates:shell-docker@sha256:1560168ac5fb9ce23d413c878349334c5845c07e264cd675d7867f0c78ad1761",
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
