"""Formatting the immutable submission context for agent prompts."""

from __future__ import annotations

import json

from nika.workflows.agent.submission import load_submission_context


def submission_prompt_context(session_id: str) -> str:
    context = load_submission_context(session_id)
    return "Frozen submission context (do not query the network):\n" + json.dumps(
        {
            "fault_ontology": context["fault_ontology"],
            "resources": context["resources"],
        },
        sort_keys=True,
    )
