"""Map agent types to native Docker Sandboxes agents."""

from __future__ import annotations

from nika.agents.sandbox.templates import TEMPLATE_IMAGES, agent_template


def native_sbx_agent(agent_type: str) -> str:
    template = agent_template(agent_type)
    if template is None:
        raise ValueError(f"No native sbx agent for {agent_type!r}")
    return template


def uses_native_sbx_agent(agent_type: str) -> bool:
    return agent_template(agent_type) is not None


def sbx_template_image(native_agent: str) -> str:
    try:
        return TEMPLATE_IMAGES[native_agent]
    except KeyError as exc:
        raise ValueError(f"No sbx template image for {native_agent!r}") from exc


def required_sbx_template_images(*agent_types: str) -> list[str]:
    """Return unique template images for sandboxed agent types."""
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
