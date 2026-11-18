"""Pinned sandbox templates shared by every sandbox backend.

Each sandbox-supported agent runs on one of Docker's sandbox agent templates.
Docker Sandboxes runs them as native agent templates; the e2b backend builds
an E2B template from the same image. Images are pinned by digest so sandboxed
runs are reproducible.
"""

from __future__ import annotations


def agent_template(agent_type: str) -> str | None:
    """Template of *agent_type* (also the sbx native agent name), if sandboxed."""
    from nika.agents.registry import find_agent_spec

    spec = find_agent_spec(agent_type)
    return spec.template if spec is not None else None


# Pinned image per template (Docker Hub ``docker/sandbox-templates``).
TEMPLATE_IMAGES: dict[str, str] = {
    "codex": "docker/sandbox-templates:codex-docker@sha256:8b4cd0a46c8b600bc6b6a64af23c03d4c2807fbfc61f47568092a93fb9dc88b0",
    "claude": "docker/sandbox-templates:claude-code-docker@sha256:549730947ed8a43182547bb5628245fe6d6c089564dd73d2194e899f791d970f",
    "shell": "docker/sandbox-templates:shell-docker@sha256:1560168ac5fb9ce23d413c878349334c5845c07e264cd675d7867f0c78ad1761",
}
