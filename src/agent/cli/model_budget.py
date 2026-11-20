"""Identify a model request budget enforced by the sandbox host."""

import os

ENV_HOST_MODEL_BUDGET = "AGENT_HOST_MODEL_BUDGET"


def host_enforces_model_budget() -> bool:
    """Whether the host proxy, rather than CLI events, bounds requests."""
    return os.environ.get(ENV_HOST_MODEL_BUDGET) == "1"
