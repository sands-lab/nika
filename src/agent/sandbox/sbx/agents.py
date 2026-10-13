"""Map NIKA agent types to native Docker Sandboxes agents."""

from __future__ import annotations

from agent.sandbox.templates import AGENT_TEMPLATES, TEMPLATE_IMAGES


def native_sbx_agent(agent_type: str) -> str:
    try:
        return AGENT_TEMPLATES[agent_type]
    except KeyError as exc:
        raise ValueError(f"No native sbx agent for {agent_type!r}") from exc


def uses_native_sbx_agent(agent_type: str) -> bool:
    return agent_type in AGENT_TEMPLATES


def sbx_template_image(native_agent: str) -> str:
    try:
        return TEMPLATE_IMAGES[native_agent]
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
