"""Pinned sandbox templates shared by every sandbox backend.

Each sandbox-supported agent runs on one of Docker's sandbox agent templates.
Docker Sandboxes runs them as native agent templates; the e2b backend builds
an E2B template from the same image. Images are pinned by digest so sandboxed
runs are reproducible.
"""

from __future__ import annotations

from agent.registry import BUILTIN_AGENTS

# Template per sandbox-supported NIKA agent (also the sbx native agent name).
AGENT_TEMPLATES: dict[str, str] = {
    spec.id: spec.sbx.native for spec in BUILTIN_AGENTS if spec.sbx is not None
}

# Pinned image per template (Docker Hub ``docker/sandbox-templates``).
TEMPLATE_IMAGES: dict[str, str] = {
    "codex": "docker/sandbox-templates:codex-docker@sha256:8b4cd0a46c8b600bc6b6a64af23c03d4c2807fbfc61f47568092a93fb9dc88b0",
    "claude": "docker/sandbox-templates:claude-code-docker@sha256:549730947ed8a43182547bb5628245fe6d6c089564dd73d2194e899f791d970f",
    "shell": "docker/sandbox-templates:shell-docker@sha256:1560168ac5fb9ce23d413c878349334c5845c07e264cd675d7867f0c78ad1761",
}
