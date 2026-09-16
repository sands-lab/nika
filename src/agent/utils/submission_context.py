"""Formatting the immutable submission context for agent prompts."""

from __future__ import annotations

import json
import os

from agent.sandbox.config import ENV_SANDBOX_EXECUTION, ENV_SESSION_DIR
from agent.sandbox.manifest import load_sandbox_manifest


def submission_prompt_context(session_id: str) -> str:
    """Return frozen fault catalog text for the submission prompt.

    Inside an SDK microVM (no ``nika`` package), use the catalog baked into
    ``sandbox_manifest.json`` by the host. On the host, load via SessionStore.
    """
    if os.environ.get(ENV_SANDBOX_EXECUTION, "").strip() == "1":
        workspace = os.environ.get(ENV_SESSION_DIR, "").strip() or "."
        baked = load_sandbox_manifest(workspace).get("submission_context")
        if isinstance(baked, dict) and baked:
            return "Frozen submission context (do not query the network):\n" + json.dumps(
                {
                    "fault_ontology": baked.get("fault_ontology", []),
                    "resources": baked.get("resources", []),
                },
                sort_keys=True,
            )
        # Host-side sandbox CLI still has ``nika``; fall through.

    from nika.workflows.agent.submission import load_submission_context

    context = load_submission_context(session_id)
    return "Frozen submission context (do not query the network):\n" + json.dumps(
        {
            "fault_ontology": context["fault_ontology"],
            "resources": context["resources"],
        },
        sort_keys=True,
    )
