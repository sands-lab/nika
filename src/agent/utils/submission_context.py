"""Formatting the immutable submission context for agent prompts."""

from __future__ import annotations

import json

from agent.utils.template import SUBMIT_PROMPT_TEMPLATE


def submission_prompt_context(context: dict) -> str:
    """Format context returned after the gateway enters submission."""
    return "Frozen submission context (do not query the network):\n" + json.dumps(
        {
            "fault_ontology": context["fault_ontology"],
            "resources": context["resources"],
        },
        sort_keys=True,
    )


def submission_user_prompt(diagnosis_report: str, context: dict) -> str:
    """User turn of the submission phase (``SUBMIT_PROMPT_TEMPLATE`` is the system prompt)."""
    return (
        f"Based on the diagnosis report: {diagnosis_report}\n"
        f"{submission_prompt_context(context)}\n"
        "Please provide the submission. Do not submit if no report is available."
    )


def submission_single_prompt(diagnosis_report: str, context: dict) -> str:
    """Submission prompt for agents without a separate system prompt (CLI)."""
    return (
        f"{SUBMIT_PROMPT_TEMPLATE}\n\n"
        f"{submission_user_prompt(diagnosis_report, context)}"
    )
